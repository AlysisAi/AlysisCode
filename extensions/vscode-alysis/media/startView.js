(function () {
  const host = window.alysisHost || acquireVsCodeApi();
  // VS Code and Cursor supply the native view title and toolbar. Only portable hosts
  // need the inline navigation; keep it hidden in the template to avoid a duplicate-header flash.
  if (window.alysisHost) {
    document.querySelector(".sidebar-top-bar")?.removeAttribute("hidden");
  }
  const taskForm = /** @type {HTMLFormElement | null} */ (document.querySelector("#taskForm"));
  const taskInput = /** @type {HTMLTextAreaElement | null} */ (document.querySelector("#taskInput"));
  const MAX_TASK_CHARS = 20000;
  const composerError = document.getElementById("composerError");
  const submitTask = /** @type {HTMLButtonElement | null} */ (document.querySelector("#submitTask"));
  const taskMode = /** @type {HTMLSelectElement | null} */ (document.querySelector("#taskMode"));
  const modeHint = document.getElementById("modeHint");
  const welcome = document.getElementById("welcome");
  const conversation = document.getElementById("conversation");
  const conversationItems = document.getElementById("conversationItems");
  const liveAnnouncer = document.getElementById("liveAnnouncer");
  const jumpToLatest = /** @type {HTMLButtonElement | null} */ (document.querySelector("#jumpToLatest"));
  const readinessStrip = document.getElementById("readinessStrip");
  const imageBasket = document.getElementById("imageBasket");
  const cancelTask = /** @type {HTMLButtonElement | null} */ (document.querySelector("#cancelTask"));
  const permissionStrip = /** @type {HTMLButtonElement | null} */ (document.querySelector("#permissionStrip"));
  const permissionLabel = document.getElementById("permissionLabel");
  const permissionDetail = document.getElementById("permissionDetail");
  const permissionPalette = document.getElementById("permissionPalette");
  const modelLabel = document.getElementById("modelLabel");
  const recentBlock = document.getElementById("recentBlock");
  const recentCards = document.getElementById("recentCards");
  const historyList = document.getElementById("historyList");
  const historySearch = /** @type {HTMLInputElement | null} */ (document.querySelector("#historySearch"));
  const historyCurrent = document.getElementById("historyCurrent");
  const settingsSearch = /** @type {HTMLInputElement | null} */ (document.querySelector("#settingsSearch"));
  const reviewAttention = document.getElementById("reviewAttention");
  const checkpointButton = document.getElementById("checkpointButton");
  let historyQuery = "";
  let historyCurrentOnly = false;
  let recentTasksSignature = "";
  const commandPalette = document.getElementById("commandPalette");
  const commandList = document.getElementById("commandList");
  const modelButton = /** @type {HTMLButtonElement | null} */ (document.querySelector("#modelButton"));
  const modelPalette = document.getElementById("modelPalette");
  const modelList = document.getElementById("modelList");
  const modelFilter = /** @type {HTMLInputElement | null} */ (document.querySelector("#modelFilter"));
  const personaButton = /** @type {HTMLButtonElement | null} */ (document.querySelector("#personaButton"));
  const personaPalette = document.getElementById("personaPalette");
  const personaList = document.getElementById("personaList");
  const personaFilter = /** @type {HTMLInputElement | null} */ (document.querySelector("#personaFilter"));
  const personaLabel = document.getElementById("personaLabel");
  const personaChip = document.getElementById("personaChip");
  const mentionPalette = document.getElementById("mentionPalette");
  const mentionList = document.getElementById("mentionList");
  const mentionHint = document.getElementById("mentionHint");
  const providerSearch = /** @type {HTMLInputElement | null} */ (document.querySelector("#providerSearch"));
  const providerList = document.getElementById("providerList");
  const catalogToggle = /** @type {HTMLButtonElement | null} */ (document.querySelector("#catalogToggle"));
  const connectionsList = document.getElementById("connectionsList");
  const modelsNotice = document.getElementById("modelsNotice");
  const connectFirst = document.getElementById("connectFirst");
  const connectFirstReason = document.getElementById("connectFirstReason");
  const starterPrompts = document.getElementById("starterPrompts");
  const browserUrl = /** @type {HTMLInputElement | null} */ (document.querySelector("#browserUrl"));
  const browserSelector = /** @type {HTMLInputElement | null} */ (document.querySelector("#browserSelector"));
  const browserTypeText = /** @type {HTMLTextAreaElement | null} */ (document.querySelector("#browserTypeText"));
  const browserReplaceText = /** @type {HTMLInputElement | null} */ (document.querySelector("#browserReplaceText"));
  const browserFullPage = /** @type {HTMLInputElement | null} */ (document.querySelector("#browserFullPage"));
  const browserSnapshotKind = /** @type {HTMLSelectElement | null} */ (document.querySelector("#browserSnapshotKind"));
  const browserSessionSelect = /** @type {HTMLSelectElement | null} */ (document.querySelector("#browserSessionSelect"));
  const savedState = /** @type {{ draft?: unknown, pendingInstruction?: unknown, pendingRequestId?: unknown, pendingMode?: unknown, pendingWorkflow?: unknown, retryInstruction?: unknown, retryRequestId?: unknown, retryMode?: unknown, retryWorkflow?: unknown, mode?: unknown, agentMode?: unknown, autoApprove?: unknown, surface?: unknown, workflow?: unknown, forgeTab?: unknown, history?: string[] } | undefined} */ (host.getState());
  const savedMode = savedState && typeof savedState.mode === "string" ? savedState.mode : "";
  const savedSurface = savedState?.surface;
  // Migrate old Plan/Act drafts without widening their permissions. The first host state wins.
  let permissionMode = ["readonly", "review", "auto"].includes(savedMode) ? savedMode
    : savedState?.agentMode === "plan" ? "readonly" : savedState?.autoApprove === true ? "auto" : "review";
  let surface = isSurface(savedSurface) ? savedSurface : "task";
  let workflow = surface === "forge" ? "forge" : "chat";
  let forgeTab = ["plan", "changes", "files", "activity"].includes(String(savedState?.forgeTab)) ? String(savedState?.forgeTab) : "plan";
  let taskPending = false;
  /** The submitted text is kept off-screen until the host accepts or rejects it. */
  let pendingInstruction = savedState && typeof savedState.pendingInstruction === "string"
    ? savedState.pendingInstruction.slice(0, 20000)
    : "";
  let pendingRequestId = savedState && typeof savedState.pendingRequestId === "string"
    ? savedState.pendingRequestId.slice(0, 128)
    : "";
  let pendingMode = savedState && ["readonly", "review", "auto"].includes(String(savedState.pendingMode))
    ? String(savedState.pendingMode)
    : "review";
  let pendingWorkflow = savedState?.pendingWorkflow === "forge" ? "forge" : "chat";
  let retryInstruction = savedState && typeof savedState.retryInstruction === "string"
    ? savedState.retryInstruction.slice(0, 20000)
    : "";
  let retryRequestId = savedState && typeof savedState.retryRequestId === "string"
    ? savedState.retryRequestId.slice(0, 128)
    : "";
  let retryMode = savedState && ["readonly", "review", "auto"].includes(String(savedState.retryMode))
    ? String(savedState.retryMode)
    : "review";
  let retryWorkflow = savedState?.retryWorkflow === "forge" ? "forge" : "chat";
  let taskAckTimer = 0;
  let taskRunning = false;
  let forgeRunning = false;
  let modeControlSignature = "";
  let settingsFilterSignature = "";
  let readyGateSignature = "";
  // These capability controls belong to the static sidebar shell. Querying the
  // entire growing transcript for them on every token wastes rendering time.
  const forgeControls = document.querySelectorAll('[data-surface="forge"], [data-forge-starter], [data-forge-draft]');
  let currentState = null;
  /** @type {Map<string, { signature: string, node: HTMLElement }>} */
  const renderedItems = new Map();
  /** The newest finalized item already spoken by the polite announcer. */
  let lastAnnouncedId = "";
  /** True while the transcript should follow new output; false the moment the user scrolls away. */
  let followTranscript = true;
  /** Signature of the last transcript tail, so "new content" can be detected while scrolled away. */
  let lastTranscriptTail = "";
  /** itemId -> epoch millis a rate limit clears, so a re-render never restarts the countdown. */
  const retryDeadlines = new Map();
  let countdownTimer = 0;
  /** Submitted instructions, newest last, for ArrowUp recall from an empty composer. */
  let history = Array.isArray(savedState?.history)
    ? savedState.history.filter((entry) => typeof entry === "string").slice(-30)
    : [];
  /** -1 while the composer holds the user's own draft; otherwise an index into `history`. */
  let historyIndex = -1;
  let historyDraft = "";
  /** @type {Array<{ name: string }>} */
  let attachedImages = [];

  // Models surface + composer picker state. modelsState mirrors the host's ModelsSurfaceState; the
  // rest is view-local so a state refresh never collapses an open form or loses a typed filter.
  let modelsState = null;
  let modelsSignature = "";
  let providerQuery = "";
  let expandedProvider = "";
  /** True once the user asks to see past the short recommended list. */
  let catalogExpanded = false;
  /** Profile name whose connection-card overflow menu is open, if any. */
  let openConnectionMenu = "";
  /** @type {Map<string, string>} */
  const providerModelDraft = new Map();
  /** @type {Map<string, string>} */
  const providerBaseUrlDraft = new Map();
  /** Preset keys whose model field is in free-text mode. */
  const providerCustomModel = new Set();
  /** Family key -> chosen transport preset key, when a provider offers more than one. */
  const providerTransport = new Map();
  // Families are derived per render pass; the host sends a fresh providers array with every state
  // post, so keying the cache on that array invalidates it exactly when the data changes.
  /** @type {{ providers: unknown, families: Array<any> }} */
  let familyCache = { providers: null, families: [] };
  let modelsRequested = false;
  let modelPaletteOpen = false;
  let modelQuery = "";
  // Composer persona picker state. personasState mirrors the host's precomputed rows (name,
  // description, clamped effective mode); the webview renders them and never re-derives modes.
  let personasState = null;
  let personaPaletteOpen = false;
  let personaQuery = "";
  /** @type {Array<{ label: string, detail: string, insert: string, kind: string }>} */
  let mentionResults = [];
  let mentionToken = 0;
  let mentionRange = null;
  let mentionTimer = 0;
  let paletteSelection = 0;
  let composerReady = false;
  /** True while the only thing blocking the composer is a check that is still running. */
  let readyGateProgress = false;
  let browserState = null;

  if (taskInput && savedState && typeof savedState.draft === "string") {
    taskInput.value = savedState.draft;
  }
  // On reload an in-flight message stays off-screen until the host acknowledges it or the bounded
  // timeout restores it. A normal state update can therefore prove acceptance even if task.result
  // was dropped, without showing an already-accepted message as a fresh draft.
  taskPending = Boolean(pendingInstruction && pendingRequestId);
  if (taskMode) taskMode.value = permissionMode;

  for (const button of document.querySelectorAll("[data-action]")) {
    button.addEventListener("click", () => {
      const action = button.getAttribute("data-action");
      if (action) {
        if (action === "new") showSurface("task");
        host.postMessage({ type: "action", action });
      }
    });
  }
  for (const button of document.querySelectorAll("[data-surface]")) {
    button.addEventListener("click", () => {
      const nextSurface = button.getAttribute("data-surface");
      if (isSurface(nextSurface)) {
        showSurface(nextSurface);
      }
    });
  }
  for (const button of document.querySelectorAll("[data-command]")) {
    button.addEventListener("click", () => {
      const command = button.getAttribute("data-command");
      if (command) {
        host.postMessage({ type: "command", command });
      }
    });
  }
  for (const button of document.querySelectorAll("[data-cockpit-action]")) {
    button.addEventListener("click", () => postCockpitAction(button.getAttribute("data-cockpit-action") || ""));
  }
  for (const button of document.querySelectorAll("[data-forge-tab]")) {
    button.addEventListener("click", () => showForgeTab(button.getAttribute("data-forge-tab") || "plan"));
    button.addEventListener("keydown", (event) => {
      if (!(event instanceof KeyboardEvent)) {
        return;
      }
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
        return;
      }
      event.preventDefault();
      const tabs = Array.from(document.querySelectorAll("[data-forge-tab]"));
      const index = tabs.indexOf(button);
      const nextIndex = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
        : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      const next = tabs[nextIndex];
      if (next instanceof HTMLElement) {
        showForgeTab(next.getAttribute("data-forge-tab") || "plan");
        next.focus();
      }
    });
  }
  for (const button of document.querySelectorAll("[data-forge-draft]")) {
    button.addEventListener("click", () => {
      showSurface("forge");
      taskInput?.focus();
    });
  }
  for (const button of document.querySelectorAll("[data-starter]")) {
    button.addEventListener("click", () => {
      if (!taskInput) {
        return;
      }
      taskInput.value = button.getAttribute("data-starter") || "";
      if (button.hasAttribute("data-forge-starter")) {
        showSurface("forge");
      }
      if (!button.hasAttribute("data-forge-starter")) {
        showSurface("task");
      }
      taskInput.focus();
      taskInput.setSelectionRange(taskInput.value.length, taskInput.value.length);
      autoGrowComposer();
      persistDraft();
      updateSubmitState();
      renderCommandPalette();
    });
  }
  for (const button of document.querySelectorAll("[data-insert]")) {
    button.addEventListener("click", () => {
      if (!taskInput) {
        return;
      }
      const insertion = button.getAttribute("data-insert") || "";
      const start = taskInput.selectionStart;
      const end = taskInput.selectionEnd;
      taskInput.setRangeText(insertion, start, end, "end");
      taskInput.focus();
      autoGrowComposer();
      persistDraft();
      updateSubmitState();
      renderCommandPalette();
      scheduleMentionSearch();
    });
  }

  taskInput?.addEventListener("input", () => {
    persistDraft();
    autoGrowComposer();
    updateSubmitState();
    renderCommandPalette();
    scheduleMentionSearch();
  });
  taskInput?.addEventListener("keydown", (event) => {
    // IME confirmation belongs to the input method, including when a suggestion menu is open.
    if (event.isComposing || event.keyCode === 229) return;
    // One keyboard contract for every composer palette: the slash catalog and the @ file picker
    // share arrow/Enter/Escape handling so neither can swallow the other's keys.
    const palette = openComposerPalette();
    if (palette) {
      const rows = paletteRows(palette);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const step = event.key === "ArrowDown" ? 1 : -1;
        paletteSelection = rows.length === 0 ? 0 : (paletteSelection + step + rows.length) % rows.length;
        updateCommandSelection(rows);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        closeComposerPalettes();
        return;
      }
      const selectedRow = rows[paletteSelection];
      const acceptsTab = palette === mentionPalette && event.key === "Tab";
      if ((event.key === "Enter" && !event.shiftKey) || acceptsTab) {
        if (selectedRow instanceof HTMLElement) {
          event.preventDefault();
          selectedRow.click();
          return;
        }
      }
    } else if (event.key === "ArrowUp" || event.key === "ArrowDown") {
      // With no palette open, the arrows recall previous submissions the way a shell does: only
      // from an empty composer (or while already browsing), so they never fight normal editing.
      if (recallHistory(event.key === "ArrowUp" ? -1 : 1)) {
        event.preventDefault();
        return;
      }
    } else if (event.key === "Escape" && historyIndex >= 0) {
      event.preventDefault();
      resetHistoryBrowsing();
      return;
    } else if (event.key === "Escape" && (taskRunning || forgeRunning) && !taskInput.value.trim()) {
      // Escape on an empty composer stops the running task, the way it does in Cline and Copilot.
      event.preventDefault();
      cancelTask?.click();
      return;
    }
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submitCurrentTask();
    }
  });
  taskInput?.addEventListener("paste", (event) => {
    const items = event.clipboardData?.items;
    if (!items) {
      return;
    }
    for (const item of Array.from(items)) {
      if (item.kind !== "file" || !item.type.startsWith("image/")) {
        continue;
      }
      const file = item.getAsFile();
      if (file) {
        event.preventDefault();
        void sendPastedImage(file);
        return;
      }
    }
  });
  for (const zone of [taskForm, conversation]) {
    zone?.addEventListener("dragover", (event) => {
      if (event.dataTransfer) {
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
        taskForm?.classList.add("drop-target");
      }
    });
    zone?.addEventListener("dragleave", () => taskForm?.classList.remove("drop-target"));
    zone?.addEventListener("drop", (event) => {
      taskForm?.classList.remove("drop-target");
      const transfer = event.dataTransfer;
      if (!transfer) {
        return;
      }
      event.preventDefault();
      const uriList = String(transfer.getData("text/uri-list") || "")
        .split(/\r?\n/)
        .map((entry) => entry.trim())
        .filter((entry) => entry && !entry.startsWith("#"));
      const images = uriList.filter((uri) => IMAGE_FILE_PATTERN.test(uri));
      if (images.length > 0) {
        host.postMessage({ type: "image.attach", uris: images.slice(0, 8) });
        return;
      }
      const files = Array.from(transfer.files || []);
      const image = files.find((file) => file.type.startsWith("image/"));
      if (image) {
        void sendPastedImage(image);
        return;
      }
      // Anything else dropped becomes an @ mention so the path lands in the task text.
      const paths = uriList.length > 0 ? uriList : files.map((file) => file.name);
      const mention = paths.map(workspaceMentionPath).filter(Boolean).map((entry) => `@${entry}`).join(" ");
      if (mention && taskInput) {
        taskInput.setRangeText(`${mention} `, taskInput.selectionStart, taskInput.selectionEnd, "end");
        taskInput.focus();
        autoGrowComposer();
        persistDraft();
        updateSubmitState();
      }
    });
  }
  taskForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    submitCurrentTask();
  });
  cancelTask?.addEventListener("click", () => {
    if (forgeRunning) {
      host.postMessage({ type: "command", command: "alysis.cancelCurrentRun" });
    } else {
      host.postMessage({ type: "task.cancel" });
    }
  });
  permissionStrip?.addEventListener("click", () => {
    if (taskRunning || forgeRunning || !permissionPalette) return;
    if (!permissionPalette.hidden) { closePermissionPalette(); return; }
    closeModelPalette();
    closePersonaPalette();
    closeComposerPalettes();
    permissionPalette.hidden = false;
    permissionStrip.setAttribute("aria-expanded", "true");
    /** @type {HTMLElement | null} */ (permissionPalette.querySelector('[aria-checked="true"]'))?.focus();
  });
  for (const button of document.querySelectorAll("[data-permission]")) {
    button.addEventListener("click", () => {
      if (taskRunning || forgeRunning || taskPending) return;
      const nextPermission = button.getAttribute("data-permission");
      if (!["readonly", "review", "auto"].includes(nextPermission || "")) return;
      closePermissionPalette();
      // Keep showing the enforced mode until the host confirms the change.
      publishModeChoice(nextPermission || "review");
      permissionStrip?.focus();
    });
  }
  permissionPalette?.addEventListener("keydown", (event) => {
    if (!(event instanceof KeyboardEvent)) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closePermissionPalette();
      permissionStrip?.focus();
    } else if (["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const options = Array.from(permissionPalette.querySelectorAll("[data-permission]"));
      const current = document.activeElement ? options.indexOf(document.activeElement) : 0;
      const index = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1 : (current + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
      /** @type {HTMLElement} */ (options[index])?.focus();
    }
  });
  document.addEventListener("pointerdown", (event) => {
    if (event.target instanceof Node && !permissionPalette?.contains(event.target) && !permissionStrip?.contains(event.target)) closePermissionPalette();
  });
  document.addEventListener("focusin", (event) => {
    if (event.target instanceof Node && !permissionPalette?.contains(event.target) && !permissionStrip?.contains(event.target)) closePermissionPalette();
  });
  historySearch?.addEventListener("input", () => {
    historyQuery = historySearch.value.trim().toLocaleLowerCase();
    renderRecentTasks(currentState?.recentTasks);
  });
  historySearch?.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      if (historyQuery) {
        historySearch.value = "";
        historyQuery = "";
        renderRecentTasks(currentState?.recentTasks);
      } else {
        showSurface("task");
      }
    }
  });
  historyCurrent?.addEventListener("click", () => {
    historyCurrentOnly = !historyCurrentOnly;
    historyCurrent.setAttribute("aria-pressed", String(historyCurrentOnly));
    renderRecentTasks(currentState?.recentTasks);
  });
  settingsSearch?.addEventListener("input", filterSettings);
  settingsSearch?.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    if (settingsSearch.value) {
      settingsSearch.value = "";
      filterSettings();
    } else {
      showSurface("task");
    }
  });
  document.getElementById("settingsClear")?.addEventListener("click", () => {
    if (settingsSearch) settingsSearch.value = "";
    filterSettings();
    settingsSearch?.focus();
  });
  reviewAttention?.addEventListener("click", () => {
    const request = currentState?.conversation?.items?.find((item) => item.kind === "approval" && item.status === "pending" && item.approvalId);
    const card = request && renderedItems.get(String(request.id))?.node;
    if (!card || !conversationItems) return;
    // Scroll only the transcript, never the outer webview and its pinned composer.
    conversationItems.scrollTop += card.getBoundingClientRect().top - conversationItems.getBoundingClientRect().top - 12;
    card.tabIndex = -1;
    card.focus({ preventScroll: true });
  });
  checkpointButton?.addEventListener("click", () => {
    const command = checkpointCommand();
    if (command) host.postMessage({ type: "command", command: command.command });
  });
  modelButton?.addEventListener("click", () => toggleModelPalette());
  modelFilter?.addEventListener("input", () => {
    modelQuery = modelFilter.value;
    renderModelPalette();
  });
  modelFilter?.addEventListener("keydown", (event) => {
    if (!(event instanceof KeyboardEvent) || !modelPalette) {
      return;
    }
    const rows = paletteRows(modelPalette);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      paletteSelection = rows.length === 0 ? 0 : (paletteSelection + step + rows.length) % rows.length;
      updateCommandSelection(rows);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closeModelPalette();
      taskInput?.focus();
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      const row = rows[paletteSelection];
      if (row instanceof HTMLElement) {
        row.click();
      }
    }
  });
  personaButton?.addEventListener("click", () => togglePersonaPalette());
  personaFilter?.addEventListener("input", () => {
    personaQuery = personaFilter.value;
    renderPersonaPalette();
  });
  // Same keyboard contract as the model palette: arrows move, Enter picks, Escape returns focus.
  personaFilter?.addEventListener("keydown", (event) => {
    if (!(event instanceof KeyboardEvent) || !personaPalette) {
      return;
    }
    const rows = paletteRows(personaPalette);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      paletteSelection = rows.length === 0 ? 0 : (paletteSelection + step + rows.length) % rows.length;
      updateCommandSelection(rows);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closePersonaPalette();
      taskInput?.focus();
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      const row = rows[paletteSelection];
      if (row instanceof HTMLElement) {
        row.click();
      }
    }
  });
  providerSearch?.addEventListener("input", () => {
    providerQuery = providerSearch.value;
    renderProviderCatalog();
  });
  catalogToggle?.addEventListener("click", () => {
    catalogExpanded = !catalogExpanded;
    renderProviderCatalog();
  });
  for (const button of document.querySelectorAll("[data-models-refresh]")) {
    button.addEventListener("click", () => {
      modelsRequested = false;
      requestModels();
    });
  }
  // A click anywhere outside the model popover dismisses it, matching every other floating menu.
  document.addEventListener("click", (event) => {
    const target = event.target instanceof Node ? event.target : null;
    // Same contract for the connection-card overflow menus: a click outside the open menu (or on a
    // different card's trigger, handled by the toggle itself) closes it.
    if (openConnectionMenu) {
      const element = event.target instanceof Element ? event.target : null;
      if (!element?.closest(".connection-overflow")) {
        openConnectionMenu = "";
        renderConnections();
      }
    }
    if (personaPaletteOpen && !(target && (personaPalette?.contains(target) || personaButton?.contains(target)))) {
      closePersonaPalette();
    }
    if (!modelPaletteOpen) {
      return;
    }
    if (target && (modelPalette?.contains(target) || modelButton?.contains(target))) {
      return;
    }
    closeModelPalette();
  });
  document.addEventListener("keydown", (event) => {
    if (!(event instanceof KeyboardEvent) || !openConnectionMenu) return;
    const profile = openConnectionMenu;
    if (event.key === "Escape") {
      event.preventDefault();
      openConnectionMenu = "";
      renderConnections();
      focusConnectionControl(profile, false);
    } else if (event.target instanceof Element && event.target.closest(".connection-menu")) {
      const menu = event.target.closest(".connection-menu");
      const rows = Array.from(menu?.querySelectorAll("button:not(:disabled)") || []);
      const index = rows.indexOf(event.target);
      const nextIndex = event.key === "Home" ? 0 : event.key === "End" ? rows.length - 1
        : event.key === "ArrowDown" ? (index + 1) % rows.length
          : event.key === "ArrowUp" ? (index + rows.length - 1) % rows.length : -1;
      if (nextIndex >= 0 && rows[nextIndex] instanceof HTMLElement) {
        event.preventDefault();
        rows[nextIndex].focus();
      } else if (event.key === "Tab") {
        openConnectionMenu = "";
        renderConnections();
        focusConnectionControl(profile, false);
      }
    }
  });
  // Scrolling away from the bottom is a deliberate act: the transcript stops following output until
  // the user comes back (or presses Jump to latest), even while a task is running.
  conversationItems?.addEventListener("scroll", () => {
    const atBottom = isNearBottom(conversationItems, 56);
    followTranscript = atBottom;
    if (atBottom) {
      setJumpToLatest(false);
    }
  });
  if (conversationItems && typeof ResizeObserver !== "undefined") {
    new ResizeObserver(() => {
      if (followTranscript && conversationItems) conversationItems.scrollTop = conversationItems.scrollHeight;
    }).observe(conversationItems);
  }
  for (const button of document.querySelectorAll("[data-worktree]")) {
    button.addEventListener("click", () => host.postMessage({ type: "worktree", action: button.getAttribute("data-worktree") }));
  }
  if (taskForm && typeof ResizeObserver !== "undefined") {
    new ResizeObserver(sizeComposerPalettes).observe(taskForm);
  }
  jumpToLatest?.addEventListener("click", () => {
    if (!conversationItems) {
      return;
    }
    followTranscript = true;
    conversationItems.scrollTop = conversationItems.scrollHeight;
    setJumpToLatest(false);
  });
  conversationItems?.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target.closest("[data-approval-decision]") : null;
    const approvalId = target?.getAttribute("data-approval-id");
    const decision = target?.getAttribute("data-approval-decision");
    if (approvalId && ["allow_once", "allow_for_session", "deny"].includes(decision || "")) {
      host.postMessage({ type: "approval", approvalId, decision });
    }
  });
  document.getElementById("forgeWorkspace")?.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target.closest("button") : null;
    if (!target) {
      return;
    }
    const action = target.getAttribute("data-dynamic-cockpit-action");
    if (action) {
      postCockpitAction(action, target);
      return;
    }
    const command = target.getAttribute("data-dynamic-command");
    if (command) {
      host.postMessage({ type: "command", command });
    }
  });
  document.getElementById("browserRefresh")?.addEventListener("click", () => postBrowserAction({ type: "browser.refresh" }));
  document.getElementById("browserStart")?.addEventListener("click", () => postBrowserAction({ type: "browser.start" }));
  document.getElementById("browserStartLocal")?.addEventListener("click", () => postBrowserAction({ type: "browser.startLocal" }));
  document.getElementById("browserClose")?.addEventListener("click", () => postBrowserAction({ type: "browser.close" }));
  document.getElementById("browserNavigate")?.addEventListener("click", () => {
    const url = browserUrl?.value.trim() || "";
    if (!url) return;
    postBrowserAction({ type: "browser.navigate", url });
    if (browserUrl) browserUrl.value = "";
    updateBrowserControls();
  });
  document.getElementById("browserClick")?.addEventListener("click", () => {
    const selector = browserSelector?.value || "";
    if (selector) postBrowserAction({ type: "browser.click", selector });
  });
  document.getElementById("browserType")?.addEventListener("click", () => {
    const selector = browserSelector?.value || "";
    const text = browserTypeText?.value || "";
    if (!selector || !text) return;
    postBrowserAction({ type: "browser.type", selector, text, replace: browserReplaceText?.checked !== false });
    if (browserTypeText) browserTypeText.value = "";
    updateBrowserControls();
  });
  document.getElementById("browserSnapshot")?.addEventListener("click", () => {
    const kind = browserSnapshotKind?.value || "text";
    if (["semantic", "accessibility", "dom", "text"].includes(kind)) {
      postBrowserAction({ type: "browser.snapshot", kind });
    }
  });
  document.getElementById("browserScreenshot")?.addEventListener("click", () => postBrowserAction({
    type: "browser.screenshot",
    fullPage: browserFullPage?.checked === true
  }));
  document.getElementById("browserDiagnostics")?.addEventListener("click", () => postBrowserAction({ type: "browser.diagnostics" }));
  document.getElementById("browserSaveScreenshot")?.addEventListener("click", () => postBrowserAction({ type: "browser.screenshot.save" }));
  browserSessionSelect?.addEventListener("change", () => {
    if (browserSessionSelect.value) {
      postBrowserAction({ type: "browser.select", browserSessionId: browserSessionSelect.value });
    }
  });
  for (const input of [browserUrl, browserSelector, browserTypeText]) {
    input?.addEventListener("input", () => updateBrowserControls());
  }

  window.addEventListener("message", (event) => {
    if (event.data?.type === "surface.show" && isSurface(event.data.surface)) {
      showSurface(event.data.surface);
      return;
    }
    if (event.data?.type === "composer.prefill" && typeof event.data.text === "string" && taskInput) {
      const currentDraft = taskInput.value.trim();
      taskInput.value = `${currentDraft ? `${currentDraft}\n\n` : ""}${event.data.text}`;
      showSurface("task");
      persistDraft();
      autoGrowComposer();
      updateSubmitState();
      taskInput.focus();
      taskInput.setSelectionRange(taskInput.value.length, taskInput.value.length);
      return;
    }
    if (event.data?.type === "mention.results") {
      applyMentionResults(Number(event.data.token), event.data.results);
      return;
    }
    if (event.data?.type === "browser.state" && event.data.state) {
      renderBrowser(event.data.state);
      return;
    }
    if (event.data?.type === "image.basket") {
      if (event.data.sessionId !== currentState?.conversation?.sessionId || !Array.isArray(event.data.names)) return;
      attachedImages = event.data.names.filter((name) => typeof name === "string").slice(0, 8)
        .map((name) => ({ name: name.slice(0, 120) }));
      renderImageBasket();
      return;
    }
    if (event.data?.type === "task.result") {
      if (event.data.requestId && pendingRequestId && event.data.requestId !== pendingRequestId) {
        return;
      }
      if (event.data.started === true) {
        acknowledgeAcceptedTask(String(event.data.requestId || pendingRequestId));
      } else {
        restorePendingTask();
      }
      return;
    }
    if (event.data?.type !== "state" || !event.data.state) {
      return;
    }
    if (taskMode && ["readonly", "review", "auto"].includes(event.data.state.mode)) {
      taskMode.value = event.data.state.mode;
      permissionMode = event.data.state.mode;
    }
    if (typeof event.data.state.lastAcceptedTaskRequestId === "string") {
      acknowledgeAcceptedTask(event.data.state.lastAcceptedTaskRequestId);
    }
    renderState(event.data.state);
  });

  function renderState(state) {
    if (currentState?.conversation?.sessionId !== state.conversation?.sessionId) {
      attachedImages = [];
      renderImageBasket();
    }
    currentState = state;
    renderWorkspaceBar(state.gitWorkspace);
    for (const control of forgeControls) {
      if (control instanceof HTMLElement) control.hidden = state.forgeEnabled === false;
    }
    if (state.forgeEnabled === false && surface === "forge") showSurface("task");
    renderConversation(state.conversation);
    renderRecentTasks(state.recentTasks);
    renderSettings(state);
    renderForge(state.forge, state.swarm);
    renderActivity(state.actionResults, state.runtimeEvents);
    renderPersona(state);
    renderCommandPalette();
    renderModels(state.models);
    renderReadyGate(state);
    renderReadinessStrip(state.readiness, Boolean(welcome && !welcome.hidden));
    if (modelLabel) {
      // Keep the model readable in a narrow composer; the tooltip retains its provider.
      const provider = providerDisplayName(String(state.providerName || "").trim());
      const model = String(state.modelName || "Default model").trim();
      modelLabel.textContent = model;
      const description = provider && provider !== "Provider" ? `${provider} · ${model}` : model;
      modelLabel.setAttribute("title", description);
      if (modelButton) modelButton.title = description;
    }
    syncWorkflowContext();
    syncModeControl();
  }

  function renderWorkspaceBar(git) {
    const bar = document.getElementById("sessionWorkspaceBar");
    if (!bar) return;
    bar.hidden = !git;
    if (!git) return;
    const disabledReason = git.busy ? "Finish or stop the current task first." : git.reason;
    for (const id of ["barNewWorktree", "barMoveWorktree", "barGitChanges"]) {
      const button = document.getElementById(id);
      if (!(button instanceof HTMLButtonElement)) continue;
      button.disabled = !git.available || (id !== "barGitChanges" && (git.busy || (id === "barMoveWorktree" && !git.canMove)));
      button.title = !git.available || git.busy ? disabledReason : id === "barMoveWorktree" ? (git.canMove ? "Copy local changes and continue this conversation in a new worktree." : "Start a conversation to move it. Requires the updated CLI.") : id === "barNewWorktree" ? "Choose a starting state and create a new branch." : `${git.files} changed files (${git.untracked} new). Line counts compare tracked files with HEAD. Choose a scope and run a structured code review with your configured provider.`;
    }
    const newSession = document.getElementById("barNewSession");
    if (newSession instanceof HTMLButtonElement) newSession.disabled = Boolean(git.busy);
    setText("barGitAdded", `+${Number(git.additions || 0).toLocaleString()}`);
    setText("barGitRemoved", `−${Number(git.deletions || 0).toLocaleString()}`);
    const detail = document.getElementById("barWorkspaceDetail");
    if (detail) {
      detail.textContent = git.available ? `${git.isWorktree ? "Worktree" : "Local"} · ${git.branch}${git.untracked ? ` · ${git.untracked} new ${git.untracked === 1 ? "file" : "files"}` : ""}` : git.reason;
      detail.title = git.root || git.reason;
    }
    bar.setAttribute("aria-busy", String(Boolean(git.busy)));
  }

  function postBrowserAction(message) {
    host.postMessage({ type: "cockpit", message });
  }

  function renderBrowser(state) {
    browserState = state;
    const sessions = Array.isArray(state.sessions) ? state.sessions : [];
    const selected = sessions.find((item) => item.browserSessionId === state.selectedBrowserId) || null;
    if (browserSessionSelect) {
      const options = sessions.map((item, index) => {
        const option = document.createElement("option");
        option.value = String(item.browserSessionId || "");
        const scopeLabel = item.networkScope === "public" ? "Shared" : "Direct IDE";
        option.textContent = `${item.product || "Managed browser"} ${index + 1} · ${scopeLabel}`;
        option.selected = option.value === state.selectedBrowserId;
        return option;
      });
      if (options.length === 0) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "No browser running";
        options.push(option);
      }
      browserSessionSelect.replaceChildren(...options);
    }
    const agentShared = !selected || selected.networkScope === "public";
    setText("browserSessionDetail", selected
      ? (agentShared
        ? "Agent-shared public session. Closing it deletes its ephemeral captures."
        : "Direct IDE session. The Alysis Code agent is isolated from this browser.")
      : "Start a public agent-shared session or a confirmed direct loopback session.");
    setText("browserPhase", friendlyBrowserPhase(state.phase, state.busy));
    setText("browserLastSafeLocation", selected?.activeUrl || "Not navigated yet");
    setText("browserNetworkAccess", selected?.networkScope === "public_loopback"
      ? "Public web + loopback only"
      : (selected?.networkScope === "local_network" ? "Legacy local scope" : "Public web only"));
    setText("browserActorAccess", agentShared ? "Alysis Code agent and IDE" : "Direct IDE only · agent blocked");
    setText("browserInteractionHint", agentShared
      ? "These controls operate the public browser shared with the Alysis Code agent."
      : "These controls operate a Direct IDE browser that the agent cannot access.");
    const localHint = document.getElementById("browserLocalHint");
    if (localHint) {
      localHint.textContent = state.localTestingSupported === true
        ? "Local testing is Direct IDE only. The agent cannot access it; LAN and link-local addresses stay blocked."
        : (state.localTestingReason || "Local testing requires a newer Alysis Code CLI.");
      localHint.setAttribute("title", localHint.textContent || "");
    }
    const status = document.getElementById("browserStatus");
    if (status) {
      status.textContent = state.error || state.notice || (!state.supported ? state.reason : "") || "";
      status.hidden = !status.textContent;
      status.classList.toggle("error", Boolean(state.error));
    }

    const previewFigure = document.getElementById("browserPreviewFigure");
    const previewImage = /** @type {HTMLImageElement | null} */ (document.querySelector("#browserPreview"));
    const hasPreview = Boolean(state.screenshot && typeof state.previewUri === "string" && state.previewUri);
    if (previewFigure) previewFigure.hidden = !hasPreview;
    if (previewImage) {
      if (hasPreview) previewImage.src = state.previewUri;
      else previewImage.removeAttribute("src");
    }
    setText("browserPreviewCaption", hasPreview
      ? `Verified PNG · ${formatBytes(state.screenshot.sizeBytes)}`
      : "");

    const snapshotDetails = /** @type {HTMLDetailsElement | null} */ (document.querySelector("#browserSnapshotDetails"));
    if (snapshotDetails) snapshotDetails.hidden = !state.snapshot;
    setText("browserSnapshotMeta", state.snapshot
      ? `${friendlySnapshotKind(state.snapshot.kind)} · ${formatBytes(state.snapshot.sizeBytes)}${state.snapshot.truncated ? " · clipped" : ""}`
      : "");
    setText("browserSnapshotOutput", state.snapshot?.preview || "");

    const diagnostics = Array.isArray(state.diagnostics) ? state.diagnostics : [];
    const diagnosticsDetails = /** @type {HTMLDetailsElement | null} */ (document.querySelector("#browserDiagnosticsDetails"));
    if (diagnosticsDetails) diagnosticsDetails.hidden = diagnostics.length === 0;
    setText("browserDiagnosticsMeta", diagnostics.length > 0
      ? `${diagnostics.length} event${diagnostics.length === 1 ? "" : "s"}${state.diagnosticsTruncated ? " · clipped" : ""}`
      : "");
    const diagnosticsOutput = document.getElementById("browserDiagnosticsOutput");
    if (diagnosticsOutput) {
      diagnosticsOutput.replaceChildren(...diagnostics.map((event) => {
        const row = document.createElement("article");
        row.className = "browser-diagnostic";
        row.append(
          textNode(event.method || (event.category === "network" ? "Network event" : "Console event"), "browser-diagnostic-title"),
          textNode(event.preview || "Details were bounded by the browser service.", "browser-diagnostic-preview")
        );
        return row;
      }));
    }
    updateBrowserControls();
  }

  function updateBrowserControls() {
    const state = browserState || {};
    const busy = Boolean(state.busy);
    const selected = Boolean(state.selectedBrowserId);
    const supported = state.supported === true;
    const trusted = state.workspaceTrusted === true;
    const setDisabled = (id, disabled) => {
      const control = /** @type {HTMLButtonElement | HTMLInputElement | HTMLSelectElement | null} */ (document.getElementById(id));
      if (control) control.disabled = disabled;
    };
    setDisabled("browserRefresh", busy || !supported);
    setDisabled("browserStart", busy || !supported || !trusted);
    setDisabled("browserStartLocal", busy || !supported || !trusted || state.localTestingSupported !== true);
    setDisabled("browserClose", busy || !selected);
    setDisabled("browserSessionSelect", busy || !selected);
    setDisabled("browserNavigate", busy || !selected || !supported || !trusted || !(browserUrl?.value.trim()));
    setDisabled("browserClick", busy || !selected || !supported || !trusted || !(browserSelector?.value));
    setDisabled("browserType", busy || !selected || !supported || !trusted || !(browserSelector?.value) || !(browserTypeText?.value));
    setDisabled("browserSnapshot", busy || !selected || !supported);
    setDisabled("browserScreenshot", busy || !selected || !supported);
    setDisabled("browserDiagnostics", busy || !selected || !supported);
    setDisabled("browserSaveScreenshot", busy || !state.screenshot || !state.previewUri);
    for (const id of ["browserUrl", "browserSelector", "browserTypeText", "browserReplaceText"]) {
      setDisabled(id, busy || !selected || !supported || !trusted);
    }
    for (const id of ["browserSnapshotKind", "browserFullPage"]) {
      setDisabled(id, busy || !selected || !supported);
    }
  }

  function friendlyBrowserPhase(phase, busy) {
    if (busy) return `Working: ${String(busy).replaceAll("_", " ")}`;
    if (phase === "ready") return "Ready";
    if (phase === "disconnected") return "Disconnected";
    if (phase === "error") return "Needs attention";
    return "Idle";
  }

  function friendlySnapshotKind(kind) {
    if (kind === "accessibility") return "Accessibility snapshot";
    if (kind === "semantic") return "Semantic snapshot";
    if (kind === "dom") return "DOM snapshot";
    return "Readable text";
  }

  function submitCurrentTask() {
    if (!taskInput || !taskMode) {
      return;
    }
    const instruction = taskInput.value.trim();
    const mode = taskMode.value;
    const runningBlocksSubmit = forgeRunning || (taskRunning && workflow !== "chat");
    if (!composerReady || taskPending || runningBlocksSubmit || !instruction || instruction.length > MAX_TASK_CHARS || !["readonly", "review", "auto"].includes(mode)) {
      return;
    }
    // Treat the click/Enter gesture as the hand-off point. Waiting for a later bridge event left
    // the sent text in the box for the whole request, which looked like Send had not worked.
    pendingInstruction = instruction;
    pendingRequestId = retryRequestId
      && retryInstruction === instruction
      && retryMode === mode
      && retryWorkflow === workflow
      ? retryRequestId
      : createRequestId();
    pendingMode = mode;
    pendingWorkflow = workflow;
    retryInstruction = "";
    retryRequestId = "";
    retryMode = "review";
    retryWorkflow = "chat";
    taskInput.value = "";
    taskPending = true;
    rememberSubmission(instruction);
    closeComposerPalettes();
    persistDraft();
    autoGrowComposer();
    updateSubmitState();
    armPendingAckTimer();
    // Show the message in the transcript the moment it is sent. Starting the engine or creating a
    // session can take many seconds; a composer that just goes blank reads as "Send did nothing".
    if (workflow === "chat") {
      renderConversation(currentState?.conversation || null);
    }
    host.postMessage({ type: "task.submit", instruction, mode, workflow, requestId: pendingRequestId });
  }

  // ---------------------------------------------------------------------------
  // Composer history recall and image attachments.
  // ---------------------------------------------------------------------------

  const IMAGE_FILE_PATTERN = /\.(png|jpe?g|gif|webp)(?:$|[?#])/i;
  const MAX_PASTED_IMAGE_BYTES = 10 * 1024 * 1024;

  /** Returns true when the arrow key was consumed by history recall. */
  function recallHistory(direction) {
    if (!taskInput || history.length === 0) {
      return false;
    }
    const value = taskInput.value;
    if (historyIndex < 0) {
      if (direction > 0 || value.trim() !== "") {
        return false;
      }
      historyDraft = value;
      historyIndex = history.length;
    }
    const nextIndex = Math.min(history.length, Math.max(0, historyIndex + direction));
    if (nextIndex === historyIndex) {
      return true;
    }
    historyIndex = nextIndex;
    taskInput.value = historyIndex >= history.length ? historyDraft : history[historyIndex];
    taskInput.setSelectionRange(taskInput.value.length, taskInput.value.length);
    if (historyIndex >= history.length) {
      historyIndex = -1;
    }
    autoGrowComposer();
    persistDraft();
    updateSubmitState();
    renderCommandPalette();
    return true;
  }

  function resetHistoryBrowsing() {
    if (!taskInput) {
      return;
    }
    historyIndex = -1;
    taskInput.value = historyDraft;
    historyDraft = "";
    autoGrowComposer();
    persistDraft();
    updateSubmitState();
  }

  function rememberSubmission(instruction) {
    history = history.filter((entry) => entry !== instruction);
    history.push(instruction);
    history = history.slice(-30);
    historyIndex = -1;
    historyDraft = "";
  }

  function workspaceMentionPath(value) {
    const raw = String(value);
    if (!/^file:\/\//i.test(raw)) return raw.replaceAll("\\", "/");
    try {
      const url = new URL(raw);
      const decoded = decodeURIComponent(url.pathname);
      if (url.hostname && url.hostname !== "localhost") return `//${url.hostname}${decoded}`;
      return decoded.replace(/^\/([A-Za-z]:\/)/, "$1");
    } catch {
      return "";
    }
  }

  async function sendPastedImage(file) {
    if (file.size > MAX_PASTED_IMAGE_BYTES) {
      if (imageBasket) {
        const notice = textNode(`Image exceeds the ${Math.round(MAX_PASTED_IMAGE_BYTES / 1048576)} MB attachment limit.`, "image-error");
        notice.setAttribute("role", "alert");
        imageBasket.replaceChildren(notice);
        imageBasket.hidden = false;
      }
      return;
    }
    const buffer = await file.arrayBuffer();
    const bytes = new Uint8Array(buffer);
    let binary = "";
    for (let index = 0; index < bytes.length; index += 1) {
      binary += String.fromCharCode(bytes[index]);
    }
    host.postMessage({
      type: "image.paste",
      name: String(file.name || "pasted-image").slice(0, 256),
      mime: file.type,
      data: btoa(binary)
    });
  }

  function renderImageBasket() {
    if (!imageBasket) {
      return;
    }
    if (attachedImages.length === 0) {
      imageBasket.replaceChildren();
      imageBasket.hidden = true;
      return;
    }
    const chips = attachedImages.map((image) => {
      const chip = document.createElement("span");
      chip.className = "image-chip";
      chip.append(iconElement("image", "image-chip-icon"), textNode(image.name, "image-chip-name"));
      return chip;
    });
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "image-basket-clear";
    clear.textContent = "Clear images";
    clear.addEventListener("click", () => {
      host.postMessage({ type: "command", command: "alysis.backend.session.images.clear" });
    });
    imageBasket.replaceChildren(textNode("Attached for the next message", "image-basket-label"), ...chips, clear);
    imageBasket.hidden = false;
  }

  function syncWorkflowContext() {
    workflow = surface === "forge" ? "forge" : "chat";
    const forge = workflow === "forge";
    if (taskInput) {
      taskInput.placeholder = !composerReady
        ? (readyGateProgress ? "Checking your connection…" : "Finish setup to start…")
        : forge
          ? "Describe what Forge should plan…"
          : taskRunning ? "Add a follow-up…" : "Ask Alysis Code…";
    }
  }

  /** @param {string} [mode] */
  function publishModeChoice(mode) {
    if (!taskMode) {
      return;
    }
    host.postMessage({ type: "cockpit", message: { type: "mode.set", mode: mode || taskMode.value } });
  }

  function syncModeControl() {
    if (!taskMode) {
      return;
    }
    taskMode.value = permissionMode;
    persistDraft();
    // Conversation, Forge, and readiness all synchronize these controls. Repainting
    // identical permissions on every token dominates the sidebar's rendering cost.
    const signature = JSON.stringify([permissionMode, taskRunning, forgeRunning, taskPending, workflow]);
    if (signature === modeControlSignature) return;
    modeControlSignature = signature;
    taskMode.disabled = taskRunning || forgeRunning || taskPending;
    if (permissionStrip) {
      // This picker controls chat permissions. Forge owns a separate review-only execution gate.
      permissionStrip.hidden = workflow === "forge";
      permissionStrip.classList.toggle("auto", permissionMode === "auto");
      permissionStrip.title = "Choose permissions";
      permissionStrip.disabled = taskRunning || forgeRunning || taskPending;
    }
    if (taskRunning || forgeRunning || taskPending) closePermissionPalette();
    for (const option of document.querySelectorAll("[data-permission]")) {
      option.setAttribute("aria-checked", String(option.getAttribute("data-permission") === permissionMode));
    }
    if (permissionLabel && permissionDetail) {
      if (permissionMode === "readonly") {
        permissionLabel.textContent = "Read-only";
        permissionDetail.textContent = "Read and analyze without changing files";
      } else if (permissionMode === "auto") {
        permissionLabel.textContent = "Auto-approve";
        permissionDetail.textContent = "Allow actions within safeguards";
      } else {
        permissionLabel.textContent = "Review changes";
        permissionDetail.textContent = "Ask before important actions";
      }
    }
    if (modeHint) {
      modeHint.textContent = taskRunning || forgeRunning
        ? "Permissions are fixed while this task is running."
        : permissionMode === "readonly"
          ? "Alysis Code can inspect and explain, but cannot change files."
          : permissionMode === "auto"
            ? "Alysis Code can proceed independently within its safety limits."
            : "You review important changes before they happen.";
    }
  }

  function renderConversation(state) {
    if (!conversation || !welcome || !conversationItems) {
      return;
    }
    state = state || { sessionId: null, mode: null, jobStatus: "", running: false, items: [] };
    renderSessionChrome({ ...currentState, conversation: state });
    const pendingEntry = pendingConversationEntry();
    const active = Boolean(state.sessionId)
      || (Array.isArray(state.items) && state.items.length > 0)
      || Boolean(pendingEntry);
    welcome.hidden = active;
    conversation.hidden = !active;
    if (!active) {
      taskRunning = false;
      renderedItems.clear();
      retryDeadlines.clear();
      conversationItems.replaceChildren();
      conversationItems.setAttribute("aria-busy", "false");
      lastAnnouncedId = "";
      lastTranscriptTail = "";
      followTranscript = true;
      setJumpToLatest(false);
      if (liveAnnouncer) {
        liveAnnouncer.textContent = "";
      }
      syncModeControl();
      updateSubmitState();
      return;
    }

    const entries = conversationEntries(state);
    if (pendingEntry) {
      entries.push(pendingEntry, {
        id: `thinking:pending:${pendingRequestId}`,
        kind: "thinking",
        status: "running",
        title: state.sessionId ? "Sending" : "Starting Alysis Code"
      });
    }
    const wasAtBottom = isNearBottom(conversationItems, 56);
    reconcileConversation(entries);
    conversationItems.setAttribute("aria-busy", state.running || Boolean(pendingEntry) ? "true" : "false");
    // The user's scroll position wins: a run no longer drags the view back to the bottom while
    // someone is reading earlier output. New content that lands off-screen offers a jump instead.
    const tail = transcriptTail(entries);
    const grew = tail !== lastTranscriptTail;
    lastTranscriptTail = tail;
    if (followTranscript || wasAtBottom) {
      followTranscript = true;
      conversationItems.scrollTop = conversationItems.scrollHeight;
      setJumpToLatest(false);
    } else if (grew) {
      setJumpToLatest(true);
    }
    announceFinalizedItem(state);
    startCountdownTicker();
    taskRunning = Boolean(state.running);
    if (state.sessionId && ["readonly", "review", "auto"].includes(state.mode)) {
      permissionMode = state.mode;
    }
    syncModeControl();
    updateSubmitState();
  }

  function checkpointCommand() {
    return currentState?.commands?.find((command) => command.available && command.command.endsWith(".checkpoint.list"));
  }

  function renderSessionChrome(state) {
    const chat = state.conversation;
    const items = Array.isArray(chat?.items) ? chat.items : [];
    const active = Boolean(chat?.sessionId || items.length || pendingInstruction);
    const header = document.getElementById("sessionHeader");
    if (header) header.hidden = !active;
    const firstPrompt = items.find((item) => item.kind === "user" && item.text)?.text;
    const remembered = state.recentTasks?.find((task) => task.current)?.title;
    const title = String(remembered || firstPrompt || pendingInstruction || "Current task").replace(/\s+/g, " ").trim();
    setText("sessionTitle", title);
    const heading = document.getElementById("sessionTitle");
    if (heading) heading.title = title;
    const pending = items.filter((item) => item.kind === "approval" && item.status === "pending" && item.approvalId);
    const status = pending.length ? "Waiting for your review" : chat?.jobStatus === "cancellation_requested" ? "Stopping…" : chat?.running ? "Working" : taskPending ? "Sending…" : ["failed", "needs_attention"].includes(chat?.jobStatus) ? "Needs attention" : chat?.jobStatus === "cancelled" ? "Stopped" : "Ready to continue";
    setText("sessionStatus", status);
    const statusNode = document.getElementById("sessionStatus");
    if (statusNode) statusNode.dataset.state = pending.length ? "attention" : chat?.running ? "working" : "ready";
    if (checkpointButton) checkpointButton.hidden = !chat?.sessionId || !checkpointCommand();
    if (reviewAttention) {
      reviewAttention.hidden = pending.length === 0;
      if (pending.length) {
        setText("reviewAttentionTitle", pending.length === 1 ? "Your review is needed" : `${pending.length} actions need your review`);
        setText("reviewAttentionDetail", pending[0].approval ? approvalPresentation(pending[0].approval).title : pending[0].title || "Inspect the proposed action");
      }
    }
  }

  /**
   * Keyed, in-place reconcile. Nodes are never detached into a fragment: an unchanged item keeps
   * its exact DOM node, so keyboard focus, text selection, and scroll anchoring survive a publish.
   * Only an item whose signature changed is rebuilt, and focus inside it is restored by key.
   */
  function reconcileConversation(entries) {
    if (!conversationItems) {
      return;
    }
    const seen = new Set();
    let cursor = null;
    for (const entry of entries) {
      const key = String(entry.id);
      seen.add(key);
      const signature = itemSignature(entry);
      let record = renderedItems.get(key);
      if (!record) {
        record = { signature, node: renderConversationItem(entry) };
        renderedItems.set(key, record);
      } else if (record.signature !== signature) {
        const replacement = renderConversationItem(entry);
        if (record.node.tagName === "DETAILS" && record.node.hasAttribute("open") && replacement.tagName === "DETAILS") {
          replacement.setAttribute("open", "");
        }
        for (const disclosure of record.node.querySelectorAll("details[data-disclosure][open]")) {
          const key = disclosure.getAttribute("data-disclosure") || "";
          replacement.querySelector(`details[data-disclosure="${cssEscape(key)}"]`)?.setAttribute("open", "");
        }
        const focusKey = activeFocusKeyWithin(record.node);
        if (entry.kind === "work") {
          updateWorkProgress(record.node, replacement);
        } else {
          if (record.node.parentNode === conversationItems) {
            conversationItems.replaceChild(replacement, record.node);
          }
          record.node = replacement;
        }
        record.signature = signature;
        restoreFocusKey(record.node, focusKey);
      }
      const reference = cursor ? cursor.nextSibling : conversationItems.firstChild;
      if (reference !== record.node) {
        conversationItems.insertBefore(record.node, reference);
      }
      cursor = record.node;
    }
    for (const [key, record] of Array.from(renderedItems.entries())) {
      if (!seen.has(key)) {
        if (record.node.parentNode === conversationItems) {
          record.node.remove();
        }
        renderedItems.delete(key);
        retryDeadlines.delete(key);
      }
    }
  }

  function activeFocusKeyWithin(node) {
    const active = document.activeElement;
    return active instanceof HTMLElement && node.contains(active) ? active.getAttribute("data-focus-key") : null;
  }

  function restoreFocusKey(node, focusKey) {
    if (!focusKey) {
      return;
    }
    const next = node.querySelector(`[data-focus-key="${cssEscape(focusKey)}"]`);
    if (next instanceof HTMLElement) {
      next.focus();
    }
  }

  function cssEscape(value) {
    return String(value).replace(/["\\]/g, "\\$&");
  }

  function isNearBottom(element, threshold) {
    if (!element) {
      return true;
    }
    return element.scrollHeight - element.scrollTop - element.clientHeight <= threshold;
  }

  function transcriptTail(entries) {
    const last = entries[entries.length - 1];
    return `${entries.length}\u0001${last ? last.id : ""}\u0001${last ? itemSignature(last).length : 0}`;
  }

  function setJumpToLatest(visible) {
    if (jumpToLatest) {
      jumpToLatest.hidden = !visible;
    }
  }

  /**
   * Announce only FINALIZED items, once each. The streaming assistant item is skipped until its
   * status settles, so a screen reader hears one complete answer instead of every token.
   */
  function announceFinalizedItem(state) {
    const items = Array.isArray(state?.items) ? state.items : [];
    for (let index = items.length - 1; index >= 0; index -= 1) {
      const item = items[index];
      if (item?.kind === "assistant" && !isCompleteStatus(item.status)) {
        continue;
      }
      if (!item || item.id === lastAnnouncedId) {
        return;
      }
      lastAnnouncedId = item.id;
      // A user message and a raw tool step are marked seen without being read back: the user typed
      // the first, and the second is machine chatter the grouped progress card already summarises.
      const text = item.kind === "user" || item.kind === "tool"
        ? ""
        : `${item.errorTitle || item.title ? `${item.errorTitle || item.title}: ` : ""}${item.errorDetail || item.text || ""}`;
      if (liveAnnouncer) {
        liveAnnouncer.textContent = clipText(text, 600);
      }
      return;
    }
  }

  function renderConversationItem(item) {
    if (item.kind === "work") {
      return renderWorkProgress(item);
    }
    if (item.kind === "thinking") {
      return renderThinkingProgress(item);
    }

    if (item.kind === "error" || item.errorKind) {
      return renderTypedErrorCard(item);
    }
    if (item.kind === "approval" && item.approval && typeof item.approval === "object") {
      return renderApprovalCard(item);
    }

    const node = document.createElement("article");
    node.dataset.itemId = item.id;
    node.className = `conversation-item message ${safeItemKind(item.kind)}${item.kind === "user" && item.status === "pending" ? " pending" : ""}`;
    if (item.kind !== "user") {
      const header = document.createElement("div");
      header.className = "message-header";
      if (item.kind === "assistant") header.classList.add("assistant-label");
      header.append(textNode(item.kind === "assistant" ? "Alysis Code" : friendlyItemLabel(item.kind, item.title), "message-author"));
      if (item.status && item.kind === "assistant" && !["complete", "completed"].includes(item.status)) {
        header.append(textNode(friendlyStatus(item.status), "message-status"));
      }
      node.append(header);
    }
    const body = document.createElement("div");
    body.className = "message-body";
    body.dir = "auto";
    renderRichText(body, item.text || (item.kind === "assistant" ? "Working..." : ""));
    node.append(body);
    if (item.kind === "assistant" && item.text && isCompleteStatus(item.status)) {
      const actions = document.createElement("div");
      actions.className = "message-actions";
      const copy = codeActionButton("Copy response", "copy", () => host.postMessage({ type: "clipboard.copy", text: item.text }), true);
      copy.title = "Copy response as Markdown";
      copy.dataset.focusKey = `copy-response:${item.id}`;
      actions.append(copy);
      node.append(actions);
    }
    if (item.kind === "approval" && item.status === "pending" && item.approvalId) {
      const actions = document.createElement("div");
      actions.className = "approval-actions";
      actions.append(approvalButton("Allow once", item.approvalId, "allow_once", false));
      if (item.allowForSession) {
        actions.append(approvalButton("Allow for task", item.approvalId, "allow_for_session", true));
      }
      actions.append(approvalButton("Deny", item.approvalId, "deny", true));
      node.append(actions);
    }
    return node;
  }

  // ---------------------------------------------------------------------------
  // Approval cards. The bridge asks before a write, a command, or a verification
  // run; the card names WHAT is being asked in the title, shows the evidence
  // (diff, command, files) as code, and offers action-specific buttons.
  // ---------------------------------------------------------------------------

  const APPROVAL_DECISION_LABELS = {
    allow_once: "Approved",
    allow_for_session: "Approved for this task",
    deny: "Denied",
    expired: "Expired without a decision"
  };

  function approvalPresentation(approval) {
    const kind = String(approval?.kind || "").toLowerCase();
    const files = Array.isArray(approval?.files) ? approval.files : [];
    const primaryFile = files.length === 1 ? compactPath(files[0]) : "";
    const hasCommand = Boolean(approval?.command);
    const preview = String(approval?.preview || "");
    const looksLikeDiff = /^(?:---|\+\+\+|@@|diff --git)/m.test(preview);
    if (kind === "workspace_trust") {
      return { title: "Allow work in this folder?", verb: "Allow", icon: "warning", evidence: "text" };
    }
    if (kind === "persona_switch") {
      return { title: "Switch persona?", verb: "Switch", icon: "circle", evidence: "text" };
    }
    if (kind === "verify_run" || /verify|test|lint|check/.test(kind)) {
      return { title: "Run verification commands?", verb: "Run", icon: "commands", evidence: hasCommand ? "command" : "text" };
    }
    if (hasCommand || /shell|exec|command|terminal|bash|powershell|run/.test(kind)) {
      return { title: "Run this command?", verb: "Run command", icon: "commands", evidence: "command" };
    }
    if (looksLikeDiff || /write|edit|patch|create|delete|move|rename|apply|file/.test(kind)) {
      const action = /delete|remove/.test(kind) ? "Delete" : /create/.test(kind) || /^\+\+\+ b\//m.test(preview) && /^--- \/dev\/null/m.test(preview) ? "Create" : "Save changes to";
      const title = primaryFile
        ? `${action} ${primaryFile}?`
        : files.length > 1
          ? `${action === "Save changes to" ? "Save changes to" : action} ${files.length} files?`
          : "Save these changes?";
      return { title, verb: action === "Delete" ? "Delete" : "Save", icon: "diff", evidence: looksLikeDiff ? "diff" : "text" };
    }
    return { title: "Approve this action?", verb: "Allow", icon: "warning", evidence: looksLikeDiff ? "diff" : hasCommand ? "command" : "text" };
  }

  function renderApprovalCard(item) {
    const approval = item.approval;
    const presentation = approvalPresentation(approval);
    const decision = String(item.status || "pending");
    const pending = decision === "pending";
    const node = document.createElement("article");
    node.dataset.itemId = item.id;
    node.className = `conversation-item message approval approval-card${pending ? " pending" : " resolved"}${decision === "deny" || decision === "expired" ? " declined" : ""}`;
    if (pending) {
      node.setAttribute("role", "group");
      node.setAttribute("aria-label", presentation.title);
    }

    const header = document.createElement("div");
    header.className = "message-header";
    const author = document.createElement("span");
    author.className = "message-author";
    author.append(
      iconElement(pending ? presentation.icon : decision === "deny" || decision === "expired" ? "circle-slash" : "check", "approval-icon"),
      document.createTextNode(pending ? presentation.title : `${presentation.title.replace(/\?$/, "")} · ${APPROVAL_DECISION_LABELS[decision] || "Resolved"}`)
    );
    header.append(author);
    if (pending && approval.expiresAt) {
      const deadline = Date.parse(approval.expiresAt);
      if (Number.isFinite(deadline) && deadline > Date.now()) {
        const expiry = document.createElement("span");
        expiry.className = "approval-expiry";
        expiry.dataset.countdownDeadline = String(deadline);
        expiry.dataset.countdownFormat = "expires";
        expiry.textContent = countdownLabel(deadline, "expires");
        header.append(expiry);
      }
    }
    node.append(header);

    const body = document.createElement("div");
    body.className = "message-body approval-body";
    if (approval.reason) {
      body.append(textNode(humanizeApprovalReason(approval.reason), "approval-reason"));
    }
    if (presentation.evidence === "command" && approval.command) {
      body.append(codeBlock(approval.command, "shell"));
    } else if (presentation.evidence === "diff" && approval.preview) {
      body.append(diffBlock(approval.preview, approval.files?.length === 1 ? String(approval.files[0]) : ""));
    } else if (approval.preview && approval.preview !== approval.reason) {
      body.append(codeBlock(approval.preview, ""));
    }
    const files = Array.isArray(approval.files) ? approval.files : [];
    if (files.length > 0 && (files.length > 1 || presentation.evidence !== "diff")) {
      const list = document.createElement("div");
      list.className = "approval-files";
      for (const file of files.slice(0, 12)) {
        list.append(fileMentionButton(String(file)) || codeSpan(String(file)));
      }
      if (files.length > 12) {
        list.append(textNode(`+${files.length - 12} more`, "approval-files-more"));
      }
      body.append(list);
    }
    if (pending && approval.warning) {
      body.append(textNode(approval.warning, "approval-warning"));
    }
    if (pending) {
      node.append(body);
    } else {
      const details = document.createElement("details");
      details.className = "approval-evidence";
      details.dataset.disclosure = "approval-evidence";
      const summary = document.createElement("summary");
      summary.textContent = "View reviewed action";
      summary.dataset.focusKey = `approval-evidence:${item.id}`;
      details.append(summary, body);
      node.append(details);
    }

    if (pending && item.approvalId) {
      const actions = document.createElement("div");
      actions.className = "approval-actions";
      actions.append(approvalButton(presentation.verb, item.approvalId, "allow_once", false));
      if (item.allowForSession) {
        actions.append(approvalButton("Allow for this task", item.approvalId, "allow_for_session", true));
      }
      actions.append(approvalButton("Deny", item.approvalId, "deny", true));
      node.append(actions);
    } else if (!pending) {
      node.setAttribute("aria-label", `${presentation.title} ${APPROVAL_DECISION_LABELS[decision] || decision}`);
    }
    return node;
  }

  /** Backend reasons read like log lines ("review mode requires confirmation for write operations"). */
  function humanizeApprovalReason(reason) {
    const text = String(reason || "").trim();
    if (!text) {
      return "";
    }
    if (/^review mode requires confirmation/i.test(text)) {
      return "You are reviewing changes, so Alysis Code asks before it writes.";
    }
    return text.charAt(0).toUpperCase() + text.slice(1).replace(/\.?$/, ".");
  }

  /** A unified diff as a scrollable block with added/removed lines tinted, never a wall of prose. */
  function diffBlock(diff, file = "") {
    const figure = document.createElement("div");
    figure.className = "diff-block";
    const pre = document.createElement("pre");
    pre.className = "diff-pre";
    pre.tabIndex = 0;
    pre.setAttribute("aria-label", "Proposed changes. Use arrow keys to scroll.");
    const lines = String(diff || "").split(/\r?\n/);
    const added = lines.filter((line) => line.startsWith("+") && !line.startsWith("+++")).length;
    const removed = lines.filter((line) => line.startsWith("-") && !line.startsWith("---")).length;
    const summary = document.createElement("div");
    summary.className = "diff-summary";
    const title = file ? fileMentionButton(file) : null;
    if (title) title.classList.add("diff-summary-title");
    summary.append(iconElement("diff"), title || textNode("Proposed changes", "diff-summary-title"), textNode(`+${added}`, "diff-stat added"), textNode(`−${removed}`, "diff-stat removed"));
    summary.setAttribute("aria-label", `Proposed changes: ${added} added lines, ${removed} removed lines in this preview`);
    figure.append(summary);
    const max = 400;
    for (const line of lines.slice(0, max)) {
      const row = document.createElement("span");
      row.className = "diff-line";
      if (/^\+\+\+|^---/.test(line)) {
        row.classList.add("diff-file");
      } else if (line.startsWith("@@")) {
        row.classList.add("diff-hunk");
      } else if (line.startsWith("+")) {
        row.classList.add("diff-add");
      } else if (line.startsWith("-")) {
        row.classList.add("diff-del");
      }
      row.textContent = line;
      pre.append(row);
    }
    if (lines.length > max) {
      pre.append(textNode(`… ${lines.length - max} more lines`, "diff-line diff-hunk"));
    }
    figure.append(pre);
    return figure;
  }

  /**
   * A typed failure renders as a card the user can act on: the host's plain-language title as the
   * heading, one sentence of detail, and its recovery actions as real buttons. A cancellation is a
   * neutral notice, not an error, because the user asked for it.
   */
  function renderTypedErrorCard(item) {
    const kind = String(item.errorKind || "");
    const cancelled = kind === "cancelled";
    const node = document.createElement("article");
    node.dataset.itemId = item.id;
    node.dataset.errorKind = kind || "unknown";
    node.className = `conversation-item message error-card ${cancelled ? "notice" : "error"}`;
    if (!cancelled) {
      node.setAttribute("role", "alert");
    }

    const header = document.createElement("div");
    header.className = "message-header";
    const author = document.createElement("span");
    author.className = "message-author";
    author.append(
      iconElement(cancelled ? "circle-slash" : "warning", "error-card-icon"),
      document.createTextNode(item.errorTitle || (cancelled ? "Stopped" : friendlyItemLabel(item.kind, item.title)))
    );
    header.append(author);
    node.append(header);

    const body = document.createElement("div");
    body.className = "message-body";
    const detail = String(item.errorDetail || item.text || "").trim();
    if (detail) {
      renderRichText(body, detail);
    }
    node.append(body);

    if (kind === "rate_limited") {
      const deadline = retryDeadline(item);
      if (deadline) {
        const countdown = document.createElement("p");
        countdown.className = "error-countdown";
        countdown.dataset.countdownDeadline = String(deadline);
        countdown.setAttribute("role", "status");
        countdown.textContent = countdownLabel(deadline);
        node.append(countdown);
      }
    }

    const actions = Array.isArray(item.errorActions) ? item.errorActions : [];
    if (actions.length > 0) {
      node.append(actionRow(actions, "error-card-actions"));
    }
    return node;
  }

  function retryDeadline(item) {
    const seconds = Number(item.retryAfterSeconds);
    if (!Number.isFinite(seconds) || seconds <= 0) {
      return 0;
    }
    const existing = retryDeadlines.get(item.id);
    if (existing) {
      return existing;
    }
    const deadline = Date.now() + seconds * 1000;
    retryDeadlines.set(item.id, deadline);
    return deadline;
  }

  function countdownLabel(deadline, format) {
    const remaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
    const minutes = Math.floor(remaining / 60);
    const seconds = remaining % 60;
    const clock = `${minutes > 0 ? `${minutes}m ` : ""}${seconds}s`;
    if (format === "expires") {
      return remaining <= 0 ? "Expired" : `Expires in ${clock}`;
    }
    if (remaining <= 0) {
      return "You can try again now.";
    }
    return `Try again in ${clock}`;
  }

  /** One shared ticker keeps every countdown honest without a timer per card. */
  function startCountdownTicker() {
    if (countdownTimer || !document.querySelector("[data-countdown-deadline]")) {
      return;
    }
    countdownTimer = setInterval(() => {
      const nodes = document.querySelectorAll("[data-countdown-deadline]");
      if (nodes.length === 0) {
        clearInterval(countdownTimer);
        countdownTimer = 0;
        return;
      }
      let pending = 0;
      for (const node of nodes) {
        const deadline = Number(node.getAttribute("data-countdown-deadline"));
        node.textContent = countdownLabel(deadline, node.getAttribute("data-countdown-format") || "");
        if (deadline > Date.now()) {
          pending += 1;
        } else {
          node.removeAttribute("data-countdown-deadline");
        }
      }
      if (pending === 0) {
        clearInterval(countdownTimer);
        countdownTimer = 0;
      }
    }, 1000);
  }

  /** Host-authored recovery actions as real buttons; every one is allowlisted on the host side. */
  function actionRow(actions, className) {
    const row = document.createElement("div");
    row.className = className;
    for (const action of actions.slice(0, 4)) {
      const command = String(action?.command || "");
      const label = String(action?.label || "");
      if (!command || !label) {
        continue;
      }
      const button = document.createElement("button");
      button.type = "button";
      button.className = action.primary ? "recovery-button primary" : "recovery-button";
      button.textContent = label;
      button.addEventListener("click", () => {
        const args = Array.isArray(action.args)
          ? action.args.filter((value) => typeof value === "string").slice(0, 4)
          : undefined;
        host.postMessage({ type: "command", command, ...(args && args.length > 0 ? { args } : {}) });
      });
      row.append(button);
    }
    return row;
  }

  function conversationEntries(state) {
    const entries = [];
    const source = Array.isArray(state?.items) ? state.items : [];
    let tools = [];

    const flushTools = () => {
      if (tools.length === 0) {
        return;
      }
      entries.push({
        id: `work:${tools[0].id}`,
        kind: "work",
        tools
      });
      tools = [];
    };

    for (const item of source) {
      if (item?.kind === "tool" && item.turnLevel === true) {
        // The turn-spanning "Working on request" activity is not a step the user took; the running
        // indicator below already says the engine is busy.
        continue;
      }
      // A provider may create the answer item before its first visible token. Keep the activity
      // indicator until actual text arrives instead of flashing an empty "Working..." message.
      if (item?.kind === "assistant" && !String(item.text || "").trim()) {
        continue;
      }
      if (item?.kind === "tool") {
        // A cancellation can end the job before a tool emits its final event. Never leave a
        // pulsing "in progress" row behind after the host has confirmed that the task stopped.
        const terminal = ["cancelled", "failed", "completed", "needs_attention"].includes(state?.jobStatus);
        tools.push(terminal && !state?.running && isActiveStatus(item.status)
          ? { ...item, status: state.jobStatus === "cancelled" ? "stopped" : "ended" }
          : item);
      } else {
        flushTools();
        entries.push(item);
      }
    }
    flushTools();

    const last = entries[entries.length - 1];
    const stopping = state?.jobStatus === "cancellation_requested";
    const hasVisibleWork = source.some((item) => item?.kind === "tool" && item.turnLevel !== true && isActiveStatus(item.status));
    const hasStreamingAnswer = last?.kind === "assistant" && !isCompleteStatus(last.status);
    const awaitingApproval = source.some((item) => item?.kind === "approval" && item.status === "pending");
    if (state?.running && (stopping || (!awaitingApproval && !hasVisibleWork && !hasStreamingAnswer))) {
      // Reuse the last work disclosure between tool calls. One row changes phase in place,
      // keeping completed steps available without stacking a second thinking indicator below it.
      if (last?.kind === "work" && (stopping || workGroupStatus(last.tools) === "complete")) {
        last.phase = stopping ? "stopping" : "thinking";
      } else {
        entries.push({
          id: `thinking:${state.sessionId || "active"}`,
          kind: "thinking",
          status: "running",
          title: stopping ? "Stopping" : "Thinking"
        });
      }
    }
    return entries;
  }

  function renderThinkingProgress(item) {
    const label = clipText(item.title || "Thinking", 60) || "Thinking";
    const node = document.createElement("div");
    node.className = "conversation-item activity-item work-progress work-progress-thinking running";
    node.dataset.itemId = item.id;
    node.setAttribute("role", "status");
    node.setAttribute("aria-label", label === "Thinking" ? "Alysis Code is thinking" : `Alysis Code: ${label}`);
    node.append(
      workMarker("running"),
      textNode(label, "work-progress-title")
    );
    return node;
  }

  /**
   * The message the user just sent, rendered before the host has confirmed it. Suppressed as soon
   * as the host's own copy of that message is in the transcript, so it never shows twice.
   */
  function pendingConversationEntry() {
    if (!taskPending || !pendingInstruction || pendingWorkflow !== "chat") {
      return null;
    }
    const items = Array.isArray(currentState?.conversation?.items) ? currentState.conversation.items : [];
    const hostHasIt = items.some((item) => item?.kind === "user" && String(item.text || "").trim() === pendingInstruction.trim());
    if (hostHasIt) {
      return null;
    }
    return {
      id: `pending:${pendingRequestId}`,
      kind: "user",
      title: "You",
      text: pendingInstruction,
      status: "pending"
    };
  }

  function renderWorkProgress(group) {
    const tools = Array.isArray(group.tools) ? group.tools : [];
    const status = group.phase ? "running" : workGroupStatus(tools);
    const activeTool = [...tools].reverse().find((tool) => isActiveStatus(tool.status)) || tools[tools.length - 1] || {};
    const presentation = toolPresentation(activeTool);
    const details = document.createElement("details");
    details.className = `conversation-item activity-item work-progress ${safeStatus(status)}`;
    details.dataset.itemId = group.id;
    details.dataset.phase = group.phase || status;

    const summary = document.createElement("summary");
    const label = group.phase === "thinking" ? "Thinking" : group.phase === "stopping" ? "Stopping" : workProgressTitle(status, presentation, tools.length);
    summary.setAttribute("aria-label", `${label}. ${stepCountLabel(tools.length)}. Show work details`);
    summary.dataset.focusKey = `work-summary:${group.id}`;
    summary.append(workMarker(status));

    const title = textNode(label, "work-progress-title");
    const titleRow = document.createElement("span");
    titleRow.className = "work-progress-title-row";
    titleRow.append(title);
    summary.append(titleRow);
    summary.append(textNode(status === "complete" ? "" : stepCountLabel(tools.length), "work-progress-count"));
    summary.append(iconElement("chevron-right", "work-progress-chevron"));

    const list = document.createElement("ol");
    list.className = "work-progress-steps";
    for (const tool of tools) {
      list.append(renderWorkStep(tool));
    }
    details.append(summary, list);
    return details;
  }

  function updateWorkProgress(current, next) {
    // Keep the disclosure, its focus, and its animated marker mounted through tool-output and
    // phase updates. Rebuilding the whole card on every event visibly resets the indicator.
    const summary = current.querySelector(":scope > summary");
    const nextSummary = next.querySelector(":scope > summary");
    const wasRunning = current.classList.contains("running");
    current.className = next.className;
    current.dataset.phase = next.dataset.phase;
    summary.setAttribute("aria-label", nextSummary.getAttribute("aria-label"));
    for (const selector of [".work-progress-title", ".work-progress-count"]) {
      const value = nextSummary.querySelector(selector).textContent;
      const target = summary.querySelector(selector);
      if (target.textContent !== value) target.textContent = value;
    }
    if (wasRunning !== next.classList.contains("running") || !wasRunning) {
      summary.querySelector(".work-progress-marker").replaceWith(nextSummary.querySelector(".work-progress-marker"));
    }
    current.querySelector(".work-progress-steps").replaceWith(next.querySelector(".work-progress-steps"));
  }

  function renderWorkStep(tool) {
    const status = normalizedWorkStatus(tool.status);
    const presentation = toolPresentation(tool);
    const item = document.createElement("li");
    item.className = `work-step ${safeStatus(status)}`;

    const technical = document.createElement("details");
    technical.className = "work-step-technical";
    technical.dataset.disclosure = `tool:${tool.id}`;
    const main = document.createElement("summary");
    main.className = "work-step-main";
    main.dataset.focusKey = `work-technical:${tool.id}`;
    main.setAttribute("aria-label", `${presentation.title}. ${workStepStatus(status)}. Show input and output`);
    main.append(
      workStepMarker(status),
      textNode(presentation.title, "work-step-title")
    );
    const context = toolContext(tool, presentation.kind);
    if (context) {
      const label = textNode(context, "work-step-context");
      label.title = context;
      main.append(label);
    }
    main.append(textNode(workStepStatus(status), "work-step-status"), iconElement("chevron-right", "work-step-chevron"));
    const body = document.createElement("pre");
    body.textContent = technicalToolDetails(tool);
    body.tabIndex = 0;
    body.setAttribute("aria-label", `${presentation.title}: input and output`);
    technical.append(main, body);
    item.append(technical);
    return item;
  }

  function renderForge(forge, swarm) {
    const empty = document.getElementById("forgeEmpty");
    const workspace = document.getElementById("forgeWorkspace");
    if (!empty || !workspace) {
      return;
    }
    forgeRunning = Boolean(forge?.activeJobId || forge?.planning || swarm?.busy);
    const hasPlan = Boolean(forge?.plan || forge?.planning);
    empty.hidden = hasPlan;
    workspace.hidden = !hasPlan;
    if (!hasPlan) {
      setText("forgeHeaderDetail", "Plan complex work before files change.");
      syncWorkflowContext();
      syncModeControl();
      updateSubmitState();
      return;
    }

    const plan = forge?.plan;
    const planning = forge?.planning;
    setText("forgeHeaderDetail", planning ? "Alysis Code is creating a reviewable plan." : "Review the plan, then preview its changes.");
    renderForgeSummary(plan, planning, forge?.executePreview);
    renderForgePlanPanel(plan, forge, swarm);
    renderForgeChanges(forge?.diffs || []);
    renderForgeFiles(forge);
    renderForgeActivity(forge, swarm);
    setText("forgePlanCount", plan?.tasks?.length ? String(plan.tasks.length) : "");
    setText("forgeDiffCount", forge?.diffs?.length ? String(forge.diffs.length) : "");
    const fileCount = countForgeFiles(forge);
    setText("forgeFileCount", fileCount ? String(fileCount) : "");
    const activityCount = (forge?.events?.length || 0) + (forge?.approvals?.length || 0);
    setText("forgeActivityCount", activityCount ? String(activityCount) : "");

    const preview = document.querySelector('[data-cockpit-action="forge.executePreview"]');
    const execute = document.querySelector('[data-cockpit-action="forge.executeReview"]');
    const runSwarm = document.querySelector('[data-cockpit-action="swarm.start"]');
    const busy = forgeRunning;
    if (preview instanceof HTMLButtonElement) {
      preview.disabled = !plan || busy;
    }
    if (execute instanceof HTMLButtonElement) {
      execute.disabled = !plan || busy;
      execute.title = forge?.executePreview?.preview_ready === false
        ? "Resolve preview blockers before running."
        : "Review scope, choose tasks, and run with approval gates.";
    }
    if (runSwarm instanceof HTMLButtonElement) {
      runSwarm.disabled = !plan || busy || swarm?.supported !== true;
      runSwarm.title = swarm?.supported === true ? "Run plan tasks across parallel workers." : (swarm?.reason || "Parallel Forge is unavailable in this CLI.");
    }
    syncWorkflowContext();
    syncModeControl();
    updateSubmitState();
    showForgeTab(forgeTab);
  }

  function renderForgeSummary(plan, planning, preview) {
    const root = document.getElementById("forgeSummary");
    if (!root) {
      return;
    }
    if (planning) {
      const pulse = document.createElement("span");
      pulse.className = "forge-live-dot";
      const copy = document.createElement("div");
      copy.append(textNode("Creating your Forge plan", "forge-kicker"), textNode(planning.instruction || "Analyzing the workspace and defining safe tasks.", "forge-goal"));
      root.replaceChildren(pulse, copy);
      root.classList.add("planning");
      return;
    }
    root.classList.remove("planning");
    if (!plan) {
      root.replaceChildren();
      return;
    }
    const copy = document.createElement("div");
    copy.append(
      textNode(`${friendlyStatus(plan.status)} · ${plan.tasks?.length || 0} tasks`, "forge-kicker"),
      textNode(plan.project_goal || "Forge plan", "forge-goal")
    );
    if (plan.summary) {
      copy.append(textNode(plan.summary, "forge-summary-text"));
    }
    if (Array.isArray(plan.warnings) && plan.warnings.length > 0) {
      copy.append(textNode(`${plan.warnings.length} item${plan.warnings.length === 1 ? "" : "s"} need attention`, "forge-warning-count"));
    }
    if (preview) {
      copy.append(textNode(preview.preview_ready ? "Preview ready" : "Preview has blockers", preview.preview_ready ? "forge-ready" : "forge-warning-count"));
    }
    root.replaceChildren(copy);
  }

  function renderForgePlanPanel(plan, forge, swarm) {
    const root = document.getElementById("forgePlanPanel");
    if (!root) {
      return;
    }
    const nodes = [];
    if (Array.isArray(forge?.approvals) && forge.approvals.length > 0) {
      nodes.push(sectionLabel("Waiting for you"));
      nodes.push(...forge.approvals.map(renderForgeApproval));
    }
    if (plan?.warnings?.length) {
      const warning = document.createElement("section");
      warning.className = "forge-notice warning";
      warning.append(textNode("Before you run", "forge-card-title"));
      const list = document.createElement("ul");
      for (const item of plan.warnings) {
        const li = document.createElement("li");
        li.textContent = item;
        list.append(li);
      }
      warning.append(list);
      nodes.push(warning);
    }
    if (plan?.tasks?.length) {
      nodes.push(sectionLabel("Plan tasks"));
      nodes.push(...plan.tasks.map(renderForgeTask));
    } else if (!forge?.planning) {
      nodes.push(emptyPanel("No plan tasks are available yet."));
    }
    if (forge?.executePreview) {
      nodes.push(sectionLabel("Execution preview"));
      nodes.push(renderForgePreview(forge.executePreview));
    }
    if (forge?.review || forge?.reviewBusy) {
      nodes.push(sectionLabel("Review"));
      nodes.push(renderForgeReview(forge.review, forge.reviewBusy));
    }
    if (swarm?.recovery?.supported || swarm?.recovery?.reason) {
      nodes.push(sectionLabel("Interrupted runs"));
      nodes.push(renderSwarmRecovery(swarm.recovery));
    }
    if (swarm?.status && swarm.status !== "idle") {
      nodes.push(sectionLabel("Parallel run"));
      nodes.push(renderSwarm(swarm));
    }
    root.replaceChildren(...nodes);
  }

  function renderForgeTask(task) {
    const details = document.createElement("details");
    details.className = "forge-task-card";
    const summary = document.createElement("summary");
    summary.append(
      stateMarker(task.status, `forge-task-state ${safeStatus(task.status)}`),
      textNode(task.title || task.task_id, "forge-task-title"),
      textNode(friendlyStatus(task.status), "forge-task-status")
    );
    details.append(summary);
    const body = document.createElement("div");
    body.className = "forge-task-body";
    if (task.objective) {
      body.append(textNode(task.objective, "forge-task-objective"));
    }
    const writeScope = task.file_scope?.write_scope || [];
    const estimated = task.file_scope?.estimated_files || [];
    if (writeScope.length || estimated.length) {
      body.append(metaRow("Files", [...new Set([...writeScope, ...estimated])].join(", ")));
    }
    if (task.acceptance_criteria?.length) {
      body.append(metaList("Done when", task.acceptance_criteria));
    }
    if (task.verification_commands?.length) {
      body.append(metaList("Checks", task.verification_commands, true));
    }
    const actions = document.createElement("div");
    actions.className = "forge-card-actions";
    const review = actionButton("Review task", "forge.review");
    review.dataset.taskId = task.task_id;
    actions.append(review);
    body.append(actions);
    details.append(body);
    return details;
  }

  function renderForgePreview(preview) {
    const card = document.createElement("section");
    card.className = `forge-card readiness ${preview.preview_ready ? "ready" : "blocked"}`;
    card.append(
      textNode(preview.preview_ready ? "Ready for review" : "Resolve these blockers", "forge-card-title"),
      textNode(preview.next_recommended_action || (preview.preview_ready ? "Review scope and start when ready." : "Review missing prerequisites."), "forge-card-detail")
    );
    const issues = [...(preview.missing_prerequisites || []), ...(preview.known_risks || [])];
    if (issues.length) {
      card.append(metaList("Details", issues));
    }
    const scopes = (preview.estimated_file_scopes || []).map((item) => item.path || item.file_path || item.scope).filter(Boolean);
    if (scopes.length) {
      card.append(metaList("Expected files", scopes));
    }
    return card;
  }

  function renderForgeReview(review, busy) {
    const card = document.createElement("section");
    card.className = "forge-card";
    if (busy) {
      card.append(textNode("Reviewing changes...", "forge-card-title"), textNode("Checking the selected task against its plan and verification evidence.", "forge-card-detail"));
      return card;
    }
    card.append(
      textNode(review?.approved ? "Review passed" : "Review needs attention", "forge-card-title"),
      textNode(review?.summary || "No review summary is available.", "forge-card-detail")
    );
    if (review) {
      card.append(metaRow("Confidence", review.confidence || "Unknown"));
      card.append(metaRow("Issues", `${review.blockingIssues || 0} blocking · ${review.nonBlockingIssues || 0} non-blocking`));
    }
    return card;
  }

  function renderSwarm(swarm) {
    const card = document.createElement("section");
    card.className = "forge-card swarm-card";
    card.append(textNode(`Parallel run · ${friendlyStatus(swarm.status)}`, "forge-card-title"));
    const tasks = document.createElement("div");
    tasks.className = "swarm-tasks";
    for (const task of swarm.tasks || []) {
      const row = document.createElement("div");
      row.className = "swarm-task";
      row.append(stateMarker(task.state, "forge-task-state"), textNode(task.title || task.taskId, "forge-task-title"), textNode(friendlyStatus(task.state), "forge-task-status"));
      // Everything a reviewer needs before Apply, in the order they need it: what is in the change,
      // then what is NOT in the change, and only then the buttons that land it.
      const untracked = Array.isArray(task.untrackedFilesNotApplied) ? task.untrackedFilesNotApplied : [];
      if (untracked.length > 0 || task.untrackedNote) {
        const warning = document.createElement("div");
        warning.className = "swarm-untracked";
        warning.append(iconElement("warning", "swarm-untracked-icon"));
        const copy = document.createElement("div");
        copy.append(textNode(
          untracked.length > 0
            ? `${untracked.length} new file${untracked.length === 1 ? "" : "s"} will NOT be applied`
            : "Some worker output will not be applied",
          "swarm-untracked-title"
        ));
        if (untracked.length > 0) {
          const list = document.createElement("ul");
          list.className = "swarm-untracked-list";
          for (const file of untracked.slice(0, 8)) {
            const entry = document.createElement("li");
            entry.textContent = String(file);
            list.append(entry);
          }
          if (untracked.length > 8) {
            const more = document.createElement("li");
            more.textContent = `and ${untracked.length - 8} more`;
            list.append(more);
          }
          copy.append(list);
        }
        if (task.untrackedNote) {
          copy.append(textNode(task.untrackedNote, "swarm-untracked-note"));
        }
        warning.append(copy);
        row.append(warning);
      }
      if (task.reviewable) {
        const actions = document.createElement("div");
        actions.className = "forge-card-actions";
        if (task.diffAvailable && task.diffArtifactId) {
          const diff = actionButton("View diff", "swarm.diff.open");
          diff.dataset.taskId = task.taskId;
          diff.dataset.artifactId = task.diffArtifactId;
          diff.dataset.sessionId = swarm.sessionId || "";
          diff.title = "Review every change this worker proposes before applying it.";
          actions.append(diff);
        }
        if (!task.applied && !task.discarded) {
          const apply = actionButton("Apply", "swarm.apply");
          apply.dataset.taskId = task.taskId;
          const discard = actionButton("Discard", "swarm.discard");
          discard.dataset.taskId = task.taskId;
          actions.append(apply, discard);
        }
        row.append(actions);
      }
      tasks.append(row);
    }
    card.append(tasks);
    if (swarm.cancellable) {
      const cancel = actionButton("Stop parallel run", "swarm.cancel");
      cancel.classList.add("danger-quiet");
      card.append(cancel);
    }
    return card;
  }

  function renderSwarmRecovery(recovery) {
    const section = document.createElement("section");
    section.className = "forge-card swarm-recovery-card";
    section.setAttribute("aria-live", "polite");
    section.setAttribute("aria-busy", recovery?.status === "loading" || recovery?.status === "resuming" ? "true" : "false");

    const header = document.createElement("div");
    header.className = "swarm-recovery-header";
    const copy = document.createElement("div");
    copy.append(
      textNode("Recover parallel work", "forge-card-title"),
      textNode("Continue unfinished workers without reusing earlier permission grants.", "forge-card-detail")
    );
    const refresh = actionButton("Check again", "swarm.recovery.refresh");
    refresh.classList.add("quiet-button");
    refresh.disabled = recovery?.supported !== true || recovery?.status === "loading" || recovery?.status === "resuming";
    refresh.setAttribute("aria-label", "Refresh interrupted Forge swarm jobs");
    header.append(copy, refresh);
    section.append(header);

    if (recovery?.status === "loading") {
      section.append(textNode("Checking durable job state…", "swarm-recovery-notice"));
      return section;
    }
    const jobs = Array.isArray(recovery?.jobs) ? recovery.jobs : [];
    if (recovery?.reason) {
      section.append(textNode(recovery.reason, recovery.status === "error" ? "swarm-recovery-notice error" : "swarm-recovery-notice"));
    }
    if (jobs.length === 0) {
      if (!recovery?.reason && recovery?.status === "ready") {
        section.append(textNode("No interrupted or failed swarms are waiting for recovery.", "swarm-recovery-notice success"));
      }
      return section;
    }

    const list = document.createElement("div");
    list.className = "swarm-recovery-list";
    for (const job of jobs) {
      const item = document.createElement("article");
      item.className = "swarm-recovery-job";
      const title = document.createElement("div");
      title.className = "swarm-recovery-job-title";
      title.append(
        textNode(friendlyStatus(job.state), `swarm-recovery-state ${safeStatus(job.state)}`),
        textNode(shortValue(job.jobId), "swarm-recovery-job-id")
      );
      item.append(title);
      item.append(textNode(
        `Updated ${formatRecoveryTime(job.updatedAt)} · ${job.attempts || 0} attempt${job.attempts === 1 ? "" : "s"} · ${job.resumeCount || 0} resume${job.resumeCount === 1 ? "" : "s"}`,
        "forge-card-detail"
      ));
      if (job.errorSummary) {
        item.append(textNode(job.errorSummary, "swarm-recovery-error"));
      }
      const usage = document.createElement("div");
      usage.className = "swarm-recovery-usage";
      usage.textContent = `${job.calls || 0} model call${job.calls === 1 ? "" : "s"} · ${Number(job.totalTokens || 0).toLocaleString()} tokens recorded`;
      item.append(usage);
      const actions = document.createElement("div");
      actions.className = "forge-card-actions";
      const resume = actionButton(
        recovery.activeJobId === job.jobId ? "Resuming…" : "Review & resume",
        "swarm.recovery.resume"
      );
      resume.classList.add("accent");
      resume.dataset.jobId = job.jobId;
      resume.dataset.revision = String(job.revision);
      resume.disabled = recovery.status === "resuming";
      const dismiss = actionButton("Dismiss", "swarm.recovery.dismiss");
      dismiss.dataset.jobId = job.jobId;
      dismiss.dataset.revision = String(job.revision);
      dismiss.disabled = recovery.status === "resuming";
      dismiss.title = "Hide this version of the recovery card without deleting backend state.";
      actions.append(resume, dismiss);
      item.append(actions);
      list.append(item);
    }
    section.append(list);
    return section;
  }

  function formatRecoveryTime(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric) || numeric <= 0) {
      return "at an unknown time";
    }
    const milliseconds = numeric < 10_000_000_000 ? numeric * 1000 : numeric;
    try {
      return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(milliseconds));
    } catch {
      return "recently";
    }
  }

  function renderForgeApproval(approval) {
    const card = document.createElement("section");
    card.className = "forge-card approval-card";
    card.append(
      textNode(approval.swarmWorker ? `${approval.swarmWorker} needs approval` : "Forge needs approval", "forge-card-title"),
      textNode(approval.reason || approval.preview || approval.kind || "Review this action before continuing.", "forge-card-detail")
    );
    if (approval.command) {
      card.append(metaRow("Command", approval.command, true));
    }
    if (approval.files?.length) {
      card.append(metaList("Files", approval.files));
    }
    const actions = document.createElement("div");
    actions.className = "forge-card-actions";
    actions.append(forgeApprovalButton("Allow once", approval, "allow_once"));
    if (approval.allowForSessionSupported) {
      actions.append(forgeApprovalButton("Allow for task", approval, "allow_for_session"));
    }
    actions.append(forgeApprovalButton("Deny", approval, "deny"));
    card.append(actions);
    return card;
  }

  function renderForgeChanges(diffs) {
    const root = document.getElementById("forgeChangesPanel");
    if (!root) {
      return;
    }
    if (!diffs.length) {
      root.replaceChildren(emptyPanel("No proposed file changes yet. Run a Forge task, then refresh."));
      return;
    }
    root.replaceChildren(...diffs.map((diff) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "forge-list-row";
      button.dataset.dynamicCockpitAction = "forge.diff.open";
      button.dataset.diffId = diff.diff_id;
      button.append(textNode(diff.file_path, "forge-row-title"), textNode(`${friendlyStatus(diff.status)} · ${formatBytes(diff.size_bytes)}`, "forge-row-detail"), textNode("Open", "forge-row-action"));
      return button;
    }));
  }

  function renderForgeFiles(forge) {
    const root = document.getElementById("forgeFilesPanel");
    if (!root) {
      return;
    }
    const nodes = [];
    if (forge?.assets?.length) {
      nodes.push(sectionLabel("Plan references"));
      for (const asset of forge.assets) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "forge-list-row";
        button.dataset.dynamicCockpitAction = "forge.assets.open";
        button.dataset.assetId = asset.id;
        button.append(textNode(asset.title || asset.id, "forge-row-title"), textNode(`${asset.kind || "File"} · ${formatBytes(asset.sizeBytes || 0)}`, "forge-row-detail"), textNode("Open", "forge-row-action"));
        nodes.push(button);
      }
    }
    const groups = forge?.artifacts || [];
    const artifacts = groups.flatMap((group) => (group.artifacts || []).map((artifact) => ({ group, artifact })));
    if (artifacts.length) {
      nodes.push(sectionLabel("Results"));
      for (const { group, artifact } of artifacts) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "forge-list-row";
        button.dataset.dynamicCockpitAction = "forge.artifact.open";
        button.dataset.sessionId = group.sessionId;
        button.dataset.artifactId = artifact.artifact_id;
        button.append(textNode(artifact.path, "forge-row-title"), textNode(formatBytes(artifact.size_bytes), "forge-row-detail"), textNode("Open", "forge-row-action"));
        nodes.push(button);
      }
    }
    const manage = document.createElement("button");
    manage.type = "button";
    manage.className = "secondary-action wide";
    manage.dataset.dynamicCommand = "alysis.manageForgeAssets";
    manage.textContent = "Manage plan files and references";
    nodes.push(manage);
    root.replaceChildren(...nodes);
  }

  function renderForgeActivity(forge, swarm) {
    const root = document.getElementById("forgeActivityPanel");
    if (!root) {
      return;
    }
    const events = forge?.events || [];
    const nodes = events.map((event) => activityRow(event.label || friendlyStatus(event.type), event.description || "", event.severity));
    if (swarm?.status && swarm.status !== "idle") {
      nodes.unshift(activityRow("Parallel Forge", friendlyStatus(swarm.status), swarm.status === "failed" ? "error" : "info"));
    }
    root.replaceChildren(...(nodes.length ? nodes : [emptyPanel("Forge activity will appear here as the plan runs.")]));
  }

  function renderActivity(results, events) {
    const actionRoot = document.getElementById("actionResults");
    const eventRoot = document.getElementById("runtimeEvents");
    if (actionRoot) {
      const list = Array.isArray(results) ? results : [];
      const nodes = list.map((result) => {
        const details = document.createElement("details");
        details.className = `activity-result ${safeStatus(result.status)}`;
        const summary = document.createElement("summary");
        summary.append(stateMarker(result.status, "activity-glyph"), textNode(result.title || result.actionId, "activity-title"), textNode(friendlyStatus(result.status), "activity-status"));
        const pre = document.createElement("pre");
        pre.textContent = result.error || safeJson(result.payload);
        details.append(summary, pre);
        return details;
      });
      actionRoot.replaceChildren(sectionLabel("Action results"), ...(nodes.length ? nodes : [emptyPanel("No command results yet.")]));
    }
    if (eventRoot) {
      const list = Array.isArray(events) ? events.filter((event) => event.severity !== "info" || event.source !== "slash").slice(0, 50) : [];
      eventRoot.replaceChildren(sectionLabel("Runtime"), ...(list.length
        ? list.map((event) => activityRow(event.title || event.source || "Alysis Code", event.message || event.details || "", event.severity))
        : [emptyPanel("No warnings or errors recorded.")]));
    }
  }

  /**
   * The "/" menu lists the real slash commands the host routes (`/forge plan`, `/model`, `/doctor`, …),
   * filtered by what has been typed so far. Picking a row inserts the command; a command that takes
   * arguments leaves the caret after a trailing space so the user can keep typing, and the menu
   * closes as soon as arguments are being typed so Enter sends the message instead of re-picking.
   */
  function renderCommandPalette() {
    if (!commandPalette || !commandList || !taskInput) {
      return;
    }
    const raw = taskInput.value.trimStart();
    if (!raw.startsWith("/") || /\n/.test(raw)) {
      commandPalette.hidden = true;
      return;
    }
    // A slash command takes the whole composer, so an open @ picker is no longer relevant.
    hideMentionPalette();
    const personasReady = personasState?.supported === true && personasState?.enabled === true;
    // "/persona ..." swaps the palette to persona-name completion from the loaded list.
    if (personasReady && /^\/persona(\s|$)/i.test(raw)) {
      renderPersonaCompletion(raw);
      return;
    }
    const catalog = Array.isArray(currentState?.slashCommands) ? currentState.slashCommands : [];
    const lower = raw.toLowerCase();
    const typedArgs = /\s\S/.test(raw);
    const exact = catalog.find((entry) => {
      const command = String(entry.command || "").toLowerCase();
      return lower === command || lower.startsWith(`${command} `);
    });
    if (exact && (typedArgs || (lower === String(exact.command).toLowerCase() && !exact.takesArgs))) {
      // The command is fully typed (and arguments are under way, or none are expected): nothing
      // left to pick, so Enter must send the message rather than re-select a row.
      commandPalette.hidden = true;
      return;
    }
    const query = lower.replace(/\s+$/, "");
    const matches = catalog.filter((entry) => {
      const command = String(entry.command || "").toLowerCase();
      if (query === "/") {
        return true;
      }
      return command.startsWith(query) || command.slice(1).includes(query.slice(1));
    }).slice(0, 12);
    // Prefix matches first, then everything else, each group alphabetical.
    matches.sort((a, b) => {
      const ap = String(a.command).toLowerCase().startsWith(query) ? 0 : 1;
      const bp = String(b.command).toLowerCase().startsWith(query) ? 0 : 1;
      return ap - bp || String(a.command).localeCompare(String(b.command));
    });
    const nodes = matches.map((entry) => {
      const command = String(entry.command || "");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "command-row slash-row";
      button.setAttribute("role", "option");
      const head = document.createElement("span");
      head.className = "command-title";
      head.append(codeSpan(command));
      if (entry.takesArgs && entry.usage && String(entry.usage).length > command.length) {
        head.append(textNode(String(entry.usage).slice(command.length), "slash-usage"));
      }
      button.append(
        head,
        textNode(entry.title || "", "command-category"),
        textNode(entry.description || "", "command-description")
      );
      button.addEventListener("click", () => {
        taskInput.value = entry.takesArgs ? `${command} ` : command;
        taskInput.focus();
        taskInput.setSelectionRange(taskInput.value.length, taskInput.value.length);
        persistDraft();
        autoGrowComposer();
        updateSubmitState();
        if (entry.takesArgs) {
          commandPalette.hidden = true;
        } else {
          // Nothing to add: send it, exactly as Enter on the typed command would.
          commandPalette.hidden = true;
          submitCurrentTask();
        }
      });
      return button;
    });
    if (personasReady && (query === "/" || "/persona".startsWith(query) || "persona".includes(query.slice(1)))) {
      nodes.unshift(personaCommandRow());
    }
    setText("commandHint", nodes.length ? "↑↓ to choose · Enter to select · Esc to close" : "");
    commandList.replaceChildren(...(nodes.length ? nodes : [emptyPanel(catalog.length === 0
      ? "Slash commands become available once Alysis Code is connected."
      : "No command matches that. Type /help to list every command.")]));
    commandPalette.hidden = false;
    paletteSelection = 0;
    updateCommandSelection(Array.from(commandList.querySelectorAll(".command-row:not(:disabled)")));
  }

  /** Entry row that turns the slash palette into /persona name completion. */
  function personaCommandRow() {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "command-row";
    button.setAttribute("role", "option");
    button.append(
      textNode("Persona", "command-title"),
      textNode("Session", "command-category"),
      textNode("Switch the agent persona; it can narrow what the agent may do, never widen it.", "command-description")
    );
    button.addEventListener("click", () => {
      if (!taskInput) {
        return;
      }
      taskInput.value = "/persona ";
      persistDraft();
      autoGrowComposer();
      updateSubmitState();
      renderCommandPalette();
      taskInput.focus();
      taskInput.setSelectionRange(taskInput.value.length, taskInput.value.length);
    });
    return button;
  }

  /** Persona-name completion rows for "/persona <partial>", from the host-loaded list. */
  function renderPersonaCompletion(raw) {
    if (!commandPalette || !commandList || !taskInput) {
      return;
    }
    const query = raw.replace(/^\/persona\s*/i, "").trim().toLowerCase();
    const options = (Array.isArray(personasState?.options) ? personasState.options : [])
      .filter((persona) => !query || String(persona.name || "").toLowerCase().includes(query))
      .slice(0, 12);
    const nodes = options.map((persona) => {
      const name = String(persona.name || "");
      const meta = [
        persona.active === true ? friendlyModeLabel(persona.effectiveMode) : persona.permissionHint || "Uses your permissions",
        persona.writeScoped === true ? "limited writes" : "",
        persona.active === true ? "Current" : ""
      ].filter(Boolean).join(" · ");
      const button = document.createElement("button");
      button.type = "button";
      button.className = `command-row${persona.active === true ? " current" : ""}`;
      button.setAttribute("role", "option");
      button.append(
        textNode(name, "command-title"),
        textNode(meta, "command-category"),
        textNode(String(persona.description || ""), "command-description")
      );
      button.addEventListener("click", () => {
        taskInput.value = "";
        persistDraft();
        autoGrowComposer();
        updateSubmitState();
        commandPalette.hidden = true;
        postPersonaSet(name);
      });
      return button;
    });
    commandList.replaceChildren(...(nodes.length ? nodes : [emptyPanel("No matching persona.")]));
    commandPalette.hidden = false;
    updateCommandSelection(Array.from(commandList.querySelectorAll(".command-row:not(:disabled)")));
  }

  function updateCommandSelection(rows) {
    const index = Math.min(Math.max(paletteSelection, 0), Math.max(rows.length - 1, 0));
    paletteSelection = index;
    rows.forEach((row, position) => {
      row.classList.toggle("selected", position === index);
      row.setAttribute("aria-selected", position === index ? "true" : "false");
    });
    rows[index]?.scrollIntoView?.({ block: "nearest" });
  }

  /** The composer palette currently taking keyboard input, if any. Slash wins over mentions. */
  function openComposerPalette() {
    if (commandPalette && !commandPalette.hidden) {
      return commandPalette;
    }
    if (mentionPalette && !mentionPalette.hidden) {
      return mentionPalette;
    }
    return null;
  }

  function paletteRows(palette) {
    return Array.from(palette?.querySelectorAll(".command-row:not(:disabled)") || []);
  }

  function closeComposerPalettes() {
    if (commandPalette) {
      commandPalette.hidden = true;
    }
    hideMentionPalette();
    closePermissionPalette();
  }

  function closePermissionPalette() {
    if (permissionPalette) permissionPalette.hidden = true;
    permissionStrip?.setAttribute("aria-expanded", "false");
  }

  // ---------------------------------------------------------------------------
  // Models surface: two sections — the connections you already have, and a
  // search-first catalog of the ones you can add.
  // ---------------------------------------------------------------------------

  /** How many providers the catalog shows before the user asks for the full list. */
  const RECOMMENDED_PROVIDER_COUNT = 5;

  function renderModels(state) {
    modelsState = state || null;
    // Token streaming republishes the whole cockpit. Rebuilding unchanged provider forms and
    // picker rows can detach a focused input or even a button between pointer-down and click.
    const signature = JSON.stringify(modelsState);
    if (signature === modelsSignature) return;
    modelsSignature = signature;
    renderModelsNotice();
    renderConnections();
    renderProviderCatalog();
    if (modelPaletteOpen) {
      renderModelPalette();
    }
  }

  function renderModelsNotice() {
    if (!modelsNotice) {
      return;
    }
    const message = modelsState?.error || (modelsState && !modelsState.supported ? modelsState.reason : "");
    modelsNotice.textContent = String(message || "");
    modelsNotice.hidden = !message;
    setText("modelsHeaderDetail", modelsState?.supported === false
      ? "Provider setup needs a compatible Alysis Code CLI."
      : "Connect a provider and choose a model.");
  }

  /**
   * Every configured connection as one card with the same anatomy: name, health chip, model,
   * one primary action, and an overflow menu. The active connection is simply the first card
   * (the host already sorts it there) rather than a separate component that can drift from the
   * rows below it.
   */
  function renderConnections() {
    if (!connectionsList) {
      return;
    }
    const connections = modelsState?.connections || [];
    if (connections.length === 0) {
      connectionsList.replaceChildren(
        emptyPanel(modelsState?.loaded
          ? "No providers connected yet. Add one below to get started."
          : "Loading your connections...")
      );
      return;
    }
    connectionsList.replaceChildren(...connections.map(renderConnectionCard));
  }

  function renderConnectionCard(connection) {
    const card = document.createElement("article");
    const needsKey = !connection.hasKey;
    card.className = `connection-card${connection.active ? " active" : ""}${needsKey ? " needs-key" : ""}`;

    const head = document.createElement("div");
    head.className = "connection-head";
    if (connection.active) {
      // The tick is decorative; the same fact reaches a screen reader as words.
      head.append(textNode("Active connection. ", "sr-only"));
      const check = iconElement("check", "connection-check");
      check.setAttribute("aria-hidden", "true");
      head.append(check);
    }
    head.append(textNode(connectionDisplayName(connection.profile), "connection-name"));
    head.append(textNode(
      needsKey ? "Needs a key" : "Connected",
      `connection-chip${needsKey ? " warn" : ""}`
    ));
    card.append(head);

    const meta = document.createElement("div");
    meta.className = "connection-meta";
    // The slug is secondary text only — never the name of the thing.
    const detail = textNode(connectionSummary(connection), "connection-detail");
    detail.title = `Profile: ${connection.profile}`;
    meta.append(detail);
    card.append(meta);

    card.append(connectionModelField(connection));

    const actions = document.createElement("div");
    actions.className = "connection-actions";
    actions.append(connectionPrimaryAction(connection));
    actions.append(connectionOverflow(connection));
    actions.append(disabledWhenBusy(connection.profile, actions));
    card.append(actions);
    return card;
  }

  /**
   * The model in use, editable on the active connection. Only the active connection's model is
   * settable — the CLI applies a model to the connection in use — so the others show theirs
   * read-only instead of offering a control that would silently do nothing.
   */
  function connectionModelField(connection) {
    const wrapper = document.createElement("label");
    wrapper.className = "connection-model";
    const name = connectionDisplayName(connection.profile);
    wrapper.append(textNode(`Model for ${name}`, "sr-only"));
    const select = document.createElement("select");
    select.className = "connection-model-select";
    const models = connection.active
      ? modelsForActiveProvider(connection)
      : (connection.model ? [connection.model] : []);
    if (models.length === 0) {
      const option = document.createElement("option");
      option.textContent = "No model set";
      select.append(option);
      select.disabled = true;
    } else {
      for (const model of models) {
        const option = document.createElement("option");
        option.value = model;
        option.textContent = model;
        option.selected = model === connection.model;
        select.append(option);
      }
    }
    if (!connection.active) {
      select.disabled = true;
      select.title = `Switch to ${name} to change its model.`;
    } else if (models.length > 0) {
      select.addEventListener("change", () => {
        host.postMessage({ type: "model.set", model: select.value });
      });
    }
    wrapper.append(select);
    return wrapper;
  }

  /**
   * One primary action per card. A connection with no key is the most important thing on the
   * screen, so "Add key" is the loudest control the panel has.
   */
  function connectionPrimaryAction(connection) {
    if (!connection.hasKey) {
      const add = connectionButton("Add key", () =>
        host.postMessage({ type: "provider.key", profile: connection.profile, keyAction: "update" })
      );
      add.classList.add("connection-primary", "attention");
      return add;
    }
    if (!connection.active) {
      const use = connectionButton("Use", () =>
        host.postMessage({ type: "provider.use", profile: connection.profile })
      );
      use.classList.add("connection-primary");
      return use;
    }
    const inUse = connectionButton("In use", () => undefined);
    inUse.classList.add("connection-primary", "quiet");
    inUse.disabled = true;
    return inUse;
  }

  function connectionOverflow(connection) {
    const wrapper = document.createElement("div");
    wrapper.className = "connection-overflow";
    wrapper.dataset.profile = connection.profile;
    const open = openConnectionMenu === connection.profile;
    const name = connectionDisplayName(connection.profile);
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "connection-more";
    trigger.textContent = "⋯";
    trigger.title = `More actions for ${name}`;
    trigger.setAttribute("aria-label", trigger.title);
    trigger.setAttribute("aria-haspopup", "menu");
    trigger.setAttribute("aria-expanded", open ? "true" : "false");
    trigger.addEventListener("click", (event) => {
      event.stopPropagation();
      openConnectionMenu = open ? "" : connection.profile;
      renderConnections();
      focusConnectionControl(connection.profile, !open);
    });
    trigger.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      event.preventDefault();
      event.stopPropagation();
      openConnectionMenu = connection.profile;
      renderConnections();
      focusConnectionControl(connection.profile, true, event.key === "ArrowUp");
    });
    wrapper.append(trigger);
    if (!open) {
      return wrapper;
    }
    const menu = document.createElement("div");
    menu.className = "connection-menu";
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", `${name} connection actions`);
    menu.append(connectionMenuItem("Replace key", () =>
      host.postMessage({ type: "provider.key", profile: connection.profile, keyAction: "update" })
    ));
    if (connection.storedInVsCode) {
      menu.append(connectionMenuItem("Remove key", () =>
        host.postMessage({ type: "provider.key", profile: connection.profile, keyAction: "forget" })
      ));
    }
    // Reconnect stays reachable from the card because a connected provider no longer appears in
    // the catalog below. The profile's own base URL is passed back so a custom endpoint survives.
    const family = familyForProfile(connection.profile);
    if (family) {
      menu.append(connectionMenuItem("Reconnect...", () =>
        host.postMessage({
          type: "provider.connect",
          presetKey: family.primary.key,
          model: connection.model || "",
          baseUrl: connection.baseUrl || ""
        })
      ));
    }
    wrapper.append(menu);
    return wrapper;
  }

  function focusConnectionControl(profile, inMenu, last = false) {
    for (const wrapper of connectionsList?.querySelectorAll(".connection-overflow") || []) {
      if (!(wrapper instanceof HTMLElement) || wrapper.dataset.profile !== profile) continue;
      const nodes = wrapper.querySelectorAll(inMenu ? ".connection-menu-item" : ".connection-more");
      const target = nodes[last ? nodes.length - 1 : 0];
      if (target instanceof HTMLElement) target.focus();
    }
  }

  function connectionMenuItem(label, onClick) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "connection-menu-item";
    button.setAttribute("role", "menuitem");
    button.textContent = label;
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const profile = openConnectionMenu;
      openConnectionMenu = "";
      onClick();
      renderConnections();
      focusConnectionControl(profile, false);
    });
    return button;
  }

  function renderProviderCatalog() {
    if (!providerList) {
      return;
    }
    // A provider you already have belongs in "Your connections" — offering it again here was the
    // main reason the same name showed up three times on one screen.
    const families = providerFamilies().filter((family) => !family.connected);
    const query = providerQuery.trim().toLowerCase();
    const matches = query ? families.filter((family) => family.search.includes(query)) : families;
    // At rest a count is noise. While filtering it answers the question the user just asked.
    setText("catalogCount", query ? `${matches.length} match${matches.length === 1 ? "" : "es"}` : "");
    if (matches.length === 0) {
      providerList.replaceChildren(
        emptyPanel(modelsState?.loaded
          ? (query ? "No provider matches that search." : "Every provider Alysis Code knows about is already connected.")
          : "Loading the provider catalog...")
      );
      renderCatalogToggle(0, 0, query);
      return;
    }
    // Searching is the way to reach the long tail, so a query always shows every match.
    const short = recommendedProviders(matches);
    const visible = query || catalogExpanded ? matches : short;
    providerList.replaceChildren(...visible.map(renderProviderRow));
    renderCatalogToggle(short.length, matches.length, query);
  }

  /**
   * The short default list: the highest-ranked providers, at most one per organisation. A vendor's
   * regional endpoints sit on one registrable domain, so without this the whole list can go to a
   * single vendor listed three times. Every one of them is still one click away in the full list.
   */
  function recommendedProviders(families) {
    const seen = new Set();
    const picked = [];
    for (const family of families) {
      if (picked.length >= RECOMMENDED_PROVIDER_COUNT) {
        break;
      }
      const domain = registrableDomain(family.host);
      if (domain) {
        if (seen.has(domain)) {
          continue;
        }
        seen.add(domain);
      }
      picked.push(family);
    }
    return picked;
  }

  function registrableDomain(host) {
    const name = String(host || "").trim().toLowerCase().replace(/:\d+$/, "");
    const labels = name.split(".").filter(Boolean);
    return labels.length >= 2 ? labels.slice(-2).join(".") : name;
  }

  function renderCatalogToggle(collapsed, total, query) {
    if (!catalogToggle) {
      return;
    }
    // Keyed on what the SHORT list covers, not on what is on screen right now — keying it on the
    // latter hid the control the moment it was used, stranding the list expanded. Anything the
    // short list cannot reach keeps the control, because a silently truncated list is
    // indistinguishable from a complete one.
    const hidden = query.length > 0 || collapsed >= total;
    catalogToggle.hidden = hidden;
    if (hidden) {
      return;
    }
    catalogToggle.textContent = catalogExpanded ? "Show fewer providers" : `Show all ${total} providers`;
    catalogToggle.setAttribute("aria-expanded", catalogExpanded ? "true" : "false");
  }

  function renderProviderRow(family) {
    const card = document.createElement("div");
    card.className = "provider-card";
    const expanded = expandedProvider === family.key;
    const provider = selectedTransport(family);

    const head = document.createElement("button");
    head.type = "button";
    head.className = "provider-head";
    head.dataset.providerKey = family.key;
    head.setAttribute("aria-expanded", expanded ? "true" : "false");
    head.append(textNode(family.label, "provider-label"));
    // A native protocol is the default and gets no badge; only the exception is tagged.
    if (provider.protocolKind === "compatibility") {
      head.append(textNode("OpenAI-compatible", "provider-tag"));
    }
    head.append(textNode(family.host || (provider.needsBaseUrl ? "Needs a base URL" : ""), "provider-host"));
    const chevron = iconElement(expanded ? "chevron-down" : "chevron-right", "provider-chevron");
    chevron.setAttribute("aria-hidden", "true");
    head.append(chevron);
    head.addEventListener("click", () => {
      expandedProvider = expanded ? "" : family.key;
      renderProviderCatalog();
      // Keep keyboard users on the disclosure after rebuilding the catalog.
      for (const candidate of providerList?.querySelectorAll(".provider-head") || []) {
        if (candidate instanceof HTMLElement && candidate.dataset.providerKey === family.key) candidate.focus();
      }
    });
    card.append(head);

    if (!expanded) {
      return card;
    }

    const form = document.createElement("div");
    form.className = "provider-form";
    if (family.transports.length > 1) {
      form.append(providerTransportField(family, provider));
    }
    if (provider.warning) {
      form.append(textNode(provider.warning, "provider-warning"));
    }
    if (provider.notes) {
      form.append(textNode(provider.notes, "provider-notes"));
    }

    if (provider.needsBaseUrl) {
      form.append(providerField("Base URL", "url", providerBaseUrlDraft.get(provider.key) || "", (value) => {
        providerBaseUrlDraft.set(provider.key, value);
      }, "https://api.example.com/v1"));
    }

    form.append(providerModelField(provider));

    form.append(textNode(
      "Your API key is stored securely in VS Code, outside your project settings.",
      "provider-key-note"
    ));

    const connect = document.createElement("button");
    connect.type = "button";
    connect.className = "primary-action";
    const busyHere = modelsState?.busy === provider.key;
    connect.textContent = busyHere ? "Working..." : "Connect";
    const updateConnectState = () => {
      const hasModel = String(providerModelDraft.get(provider.key) || "").trim().length > 0;
      connect.disabled = busyHere || !hasModel;
      connect.title = hasModel ? "" : "Choose or type a model before connecting.";
    };
    updateConnectState();
    for (const field of form.querySelectorAll("input, select")) {
      field.addEventListener("input", updateConnectState);
      field.addEventListener("change", updateConnectState);
    }
    connect.addEventListener("click", () => {
      host.postMessage({
        type: "provider.connect",
        presetKey: provider.key,
        model: providerModelDraft.get(provider.key) || "",
        baseUrl: providerBaseUrlDraft.get(provider.key) || ""
      });
    });
    form.append(connect);
    if (busyHere) {
      const phase = busyPhaseText();
      if (phase) {
        form.append(textNode(phase, "provider-busy-phase"));
      }
    }
    card.append(form);
    return card;
  }

  /** Transport is an attribute of one provider, never a second row for the same provider. */
  function providerTransportField(family, provider) {
    const wrapper = document.createElement("label");
    wrapper.className = "provider-field";
    wrapper.append(textNode("Transport", "provider-field-label"));
    const select = document.createElement("select");
    select.className = "provider-input";
    for (const variant of family.transports) {
      const option = document.createElement("option");
      option.value = variant.key;
      option.textContent = transportLabel(variant.protocolKind);
      option.title = variant.label;
      option.selected = variant.key === provider.key;
      select.append(option);
    }
    select.addEventListener("change", () => {
      providerTransport.set(family.key, select.value);
      renderProviderCatalog();
    });
    wrapper.append(select);
    wrapper.append(textNode(`Connects with the ${provider.label} preset.`, "provider-transport-note"));
    return wrapper;
  }

  function providerModelField(provider) {
    const wrapper = document.createElement("label");
    wrapper.className = "provider-field";
    wrapper.append(textNode("Model", "provider-field-label"));
    // Seed the draft with the model the field actually displays, so connecting without touching
    // the dropdown sends the model the user can see rather than relying on the CLI preset default
    // happening to match the first suggestion.
    if (!providerModelDraft.has(provider.key) && provider.models.length > 0) {
      providerModelDraft.set(provider.key, provider.models[0]);
    }
    const current = providerModelDraft.get(provider.key) ?? "";
    const custom = providerCustomModel.has(provider.key);

    if (provider.models.length > 0) {
      const select = document.createElement("select");
      select.className = "provider-input";
      for (const model of provider.models) {
        const option = document.createElement("option");
        option.value = model;
        option.textContent = model;
        option.selected = !custom && model === current;
        select.append(option);
      }
      const other = document.createElement("option");
      // Flagged by attribute, not by a sentinel value: any string chosen as a sentinel could collide
      // with a real model name, and option values do not always survive a DOM round-trip intact.
      other.value = "";
      other.dataset.otherModel = "true";
      other.textContent = "Other (type a model name)";
      other.selected = custom;
      select.append(other);
      select.addEventListener("change", () => {
        // "Other" is tracked explicitly rather than inferred from an empty draft; inferring it made
        // the dropdown snap back to the first suggestion on the very next render.
        if (select.options[select.selectedIndex]?.dataset.otherModel === "true") {
          providerCustomModel.add(provider.key);
          providerModelDraft.set(provider.key, "");
        } else {
          providerCustomModel.delete(provider.key);
          providerModelDraft.set(provider.key, select.value);
        }
        renderProviderCatalog();
      });
      wrapper.append(select);
      if (!custom) {
        return wrapper;
      }
    }

    wrapper.append(providerInput("text", current, (value) => {
      providerModelDraft.set(provider.key, value);
    }, "model-name"));
    return wrapper;
  }

  function providerField(label, type, value, onInput, placeholder) {
    const wrapper = document.createElement("label");
    wrapper.className = "provider-field";
    wrapper.append(textNode(label, "provider-field-label"));
    wrapper.append(providerInput(type, value, onInput, placeholder));
    return wrapper;
  }

  function providerInput(type, value, onInput, placeholder) {
    const input = document.createElement("input");
    input.type = type;
    input.className = "provider-input";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.maxLength = 2048;
    input.value = value;
    input.placeholder = placeholder || "";
    input.addEventListener("input", () => onInput(input.value));
    return input;
  }

  function connectionButton(label, onClick) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "connection-action";
    button.textContent = label;
    button.addEventListener("click", onClick);
    return button;
  }

  // Busy is reported per target id (preset key or profile name); reflecting it on the card keeps a
  // second click from queueing a duplicate mutation while the first is still running. Only ever
  // disables: a control that is already disabled for its own reason must stay that way.
  function disabledWhenBusy(target, actions) {
    const marker = document.createElement("span");
    marker.className = "connection-busy";
    const busy = modelsState?.busy === target;
    marker.textContent = busy ? busyPhaseText() || "Working..." : "";
    marker.hidden = !busy;
    if (busy) {
      for (const button of actions.querySelectorAll("button")) {
        button.disabled = true;
      }
    }
    return marker;
  }

  /**
   * One card per provider, with transport as an attribute of that card.
   *
   * Presets that differ only by transport — a native protocol, its OpenAI-compatible twin, and the
   * "(native alias)" duplicates — describe the same endpoint, so the base URL host groups them.
   * Grouping on the host rather than on label text means a relabelled preset cannot silently split
   * a provider back into two rows.
   */
  function providerFamilies() {
    const providers = modelsState?.providers || [];
    if (familyCache.providers === providers) {
      return familyCache.families;
    }
    /** @type {Map<string, Array<object>>} */
    const grouped = new Map();
    for (const provider of providers) {
      const key = provider.host || provider.key;
      const bucket = grouped.get(key);
      if (bucket) {
        bucket.push(provider);
      } else {
        grouped.set(key, [provider]);
      }
    }
    const families = [...grouped.entries()].map(([key, variants]) => buildProviderFamily(key, variants));
    familyCache = { providers, families };
    return families;
  }

  function buildProviderFamily(key, variants) {
    // Best transport first: native beats compatibility, then the shortest preset key wins so a
    // deliberate alias never outranks the canonical entry it aliases.
    const ordered = [...variants].sort((left, right) =>
      Number(left.protocolKind !== "native") - Number(right.protocolKind !== "native") ||
      left.key.length - right.key.length ||
      left.key.localeCompare(right.key)
    );
    // Two presets with the same transport are the same choice offered twice.
    const transports = [];
    for (const variant of ordered) {
      if (!transports.some((entry) => entry.protocolKind === variant.protocolKind)) {
        transports.push(variant);
      }
    }
    // The provider's name is the plainest label any of its presets carries; the longer ones are
    // that name plus a transport suffix, which the transport control now says instead.
    const label = [...variants]
      .sort((left, right) => left.label.length - right.label.length || left.label.localeCompare(right.label))[0]
      .label;
    return {
      key,
      label,
      host: transports[0].host,
      primary: transports[0],
      transports,
      variants,
      connected: variants.some((variant) => variant.connected),
      search: `${label} ${variants.map((variant) => `${variant.label} ${variant.key}`).join(" ")} ${transports[0].host} ${transports[0].models.join(" ")}`.toLowerCase()
    };
  }

  function selectedTransport(family) {
    const chosen = providerTransport.get(family.key);
    return family.transports.find((variant) => variant.key === chosen) || family.primary;
  }

  function transportLabel(protocolKind) {
    if (protocolKind === "native") {
      return "Native API";
    }
    if (protocolKind === "compatibility") {
      return "OpenAI-compatible";
    }
    return "Other transport";
  }

  function familyForProfile(profile) {
    return providerFamilies().find((family) =>
      family.variants.some((variant) => variant.profileName === profile)
    );
  }

  /** The human name for a configured profile. The slug is secondary text, never the name. */
  function connectionDisplayName(profile) {
    const family = familyForProfile(profile);
    return family ? family.label : humanizeSlug(profile);
  }

  /**
   * Same rule for the composer chip, but a value that is not a profile at all (a configured
   * provider label, or the "Provider" placeholder) is passed through untouched.
   */
  function providerDisplayName(name) {
    const family = familyForProfile(name);
    if (family) {
      return family.label;
    }
    const configured = (modelsState?.connections || []).some((entry) => entry.profile === name);
    return configured ? humanizeSlug(name) : name;
  }

  /**
   * Title-case a profile slug for display. Profiles created outside the preset catalog (an account
   * login, a hand-written profile) have no catalog label to borrow. The small word table only fixes
   * capitalisation — it decides nothing — and an unlisted word just gets a leading capital.
   */
  const SLUG_WORDS = {
    ai: "AI",
    api: "API",
    chatgpt: "ChatGPT",
    cn: "CN",
    glm: "GLM",
    gpt: "GPT",
    llm: "LLM",
    lm: "LM",
    mcp: "MCP",
    openai: "OpenAI",
    us: "US",
    vllm: "vLLM",
    xai: "xAI"
  };

  function humanizeSlug(slug) {
    const text = String(slug || "").trim();
    if (text.length === 0) {
      return "";
    }
    return text
      .split(/[-_\s]+/)
      .filter(Boolean)
      .map((word) => SLUG_WORDS[word.toLowerCase()] || `${word.charAt(0).toUpperCase()}${word.slice(1)}`)
      .join(" ");
  }

  /**
   * The native key prompt opens at the top of the window, away from the card the user clicked, so
   * the busy card says exactly what is happening instead of silently disabling its buttons.
   */
  function busyPhaseText() {
    switch (modelsState?.busyPhase) {
      case "waiting_for_key":
        return "Check the top of the window — VS Code is asking for your API key.";
      case "connecting":
        return "Connecting...";
      case "checking_key":
        return "Checking your key with a quick test request...";
      default:
        return "";
    }
  }

  // Secondary line for a connection: the slug it is stored under, where it points, and whether a
  // key is saved. A native protocol is the default and says nothing here; only the exception does.
  function connectionSummary(connection) {
    const parts = [connection.profile];
    if (connection.host) {
      parts.push(connection.host);
    }
    parts.push(connection.hasKey ? "Key saved" : "No key yet");
    if (connection.protocolKind === "compatibility") {
      parts.push("OpenAI-compatible");
    }
    return parts.join(" · ");
  }

  // ---------------------------------------------------------------------------
  // Composer model picker.
  // ---------------------------------------------------------------------------

  function toggleModelPalette() {
    modelPaletteOpen = !modelPaletteOpen;
    if (modelPaletteOpen) {
      closeComposerPalettes();
      closePersonaPalette();
      requestModels();
      renderModelPalette();
      setTimeout(() => modelFilter?.focus(), 0);
    } else if (modelPalette) {
      modelPalette.hidden = true;
    }
    modelButton?.setAttribute("aria-expanded", modelPaletteOpen ? "true" : "false");
  }

  function closeModelPalette() {
    if (!modelPaletteOpen) {
      return;
    }
    modelPaletteOpen = false;
    if (modelPalette) {
      modelPalette.hidden = true;
    }
    modelButton?.setAttribute("aria-expanded", "false");
  }

  function renderModelPalette() {
    if (!modelPalette || !modelList) {
      return;
    }
    modelPalette.hidden = false;
    const query = modelQuery.trim().toLowerCase();
    const connections = modelsState?.connections || [];
    const active = connections.find((entry) => entry.active);
    const nodes = [];

    const models = modelsForActiveProvider(active);
    const matchingModels = models.filter((model) => !query || model.toLowerCase().includes(query));
    if (matchingModels.length > 0) {
      nodes.push(sectionLabel(active ? `Models · ${connectionDisplayName(active.profile)}` : "Models"));
      for (const model of matchingModels) {
        nodes.push(paletteRow(model, model === active?.model ? "Current" : "", "", () => {
          closeModelPalette();
          host.postMessage({ type: "model.set", model });
        }, model === active?.model));
      }
    }

    const others = connections.filter((entry) =>
      !entry.active && (!query || `${entry.profile} ${entry.model}`.toLowerCase().includes(query))
    );
    if (others.length > 0) {
      nodes.push(sectionLabel("Switch provider"));
      for (const connection of others) {
        nodes.push(paletteRow(connectionDisplayName(connection.profile), connection.model || "", connectionSummary(connection), () => {
          closeModelPalette();
          host.postMessage({ type: "provider.use", profile: connection.profile });
        }, false));
      }
    }

    if (nodes.length === 0) {
      nodes.push(emptyPanel(modelsState?.loaded
        ? "No connected providers yet. Use Manage providers to add one."
        : "Loading providers..."));
    }
    modelList.replaceChildren(...nodes);
    paletteSelection = 0;
    updateCommandSelection(paletteRows(modelPalette));
  }

  // ---------------------------------------------------------------------------
  // Composer persona picker. Structure and keyboard contract cloned from the
  // model picker; every row's effective mode is precomputed by the host with the
  // clamp rule (a persona can narrow the session mode, never widen it).
  // ---------------------------------------------------------------------------

  function togglePersonaPalette() {
    personaPaletteOpen = !personaPaletteOpen;
    if (personaPaletteOpen) {
      closeComposerPalettes();
      closeModelPalette();
      renderPersonaPalette();
      setTimeout(() => personaFilter?.focus(), 0);
    } else if (personaPalette) {
      personaPalette.hidden = true;
    }
    personaButton?.setAttribute("aria-expanded", personaPaletteOpen ? "true" : "false");
  }

  function closePersonaPalette() {
    if (!personaPaletteOpen) {
      return;
    }
    personaPaletteOpen = false;
    if (personaPalette) {
      personaPalette.hidden = true;
    }
    personaButton?.setAttribute("aria-expanded", "false");
  }

  function renderPersonaPalette() {
    if (!personaPalette || !personaList) {
      return;
    }
    personaPalette.hidden = false;
    const query = personaQuery.trim().toLowerCase();
    const options = Array.isArray(personasState?.options) ? personasState.options : [];
    const nodes = [];
    for (const persona of options) {
      const name = String(persona.name || "");
      const description = String(persona.description || "");
      if (query && !`${name} ${description}`.toLowerCase().includes(query)) {
        continue;
      }
      const meta = [
        persona.active === true ? friendlyModeLabel(persona.effectiveMode) : persona.permissionHint || "Uses your permissions",
        persona.writeScoped === true ? "limited writes" : "",
        persona.active === true ? "Current" : ""
      ].filter(Boolean).join(" · ");
      nodes.push(paletteRow(name, meta, description, () => {
        closePersonaPalette();
        postPersonaSet(name);
        taskInput?.focus();
      }, persona.active === true));
    }
    if (nodes.length === 0) {
      nodes.push(emptyPanel(personasState && personasState.enabled === false
        ? "Personas are turned off for this Alysis Code CLI."
        : "No personas are available for this session."));
    }
    personaList.replaceChildren(...nodes);
    paletteSelection = 0;
    updateCommandSelection(paletteRows(personaPalette));
  }

  /** Validate before anything leaves the webview; the host re-validates the same pattern. */
  function postPersonaSet(name) {
    if (typeof name !== "string" || !/^[a-z0-9][a-z0-9._-]{0,63}$/i.test(name)) {
      return;
    }
    host.postMessage({ type: "cockpit", message: { type: "persona.set", name } });
  }

  function renderPersona(state) {
    personasState = state && typeof state.personas === "object" ? state.personas : null;
    const supported = personasState?.supported === true;
    const enabled = personasState?.enabled === true;
    const reason = typeof personasState?.reason === "string" ? personasState.reason : "";
    const running = state?.conversation?.running === true;
    const active = typeof personasState?.active === "string" ? personasState.active : "";
    if (personaButton) {
      if (supported && enabled) {
        personaButton.hidden = false;
        personaButton.disabled = running;
        personaButton.title = running ? "Personas switch between tasks" : "Choose persona";
        personaButton.setAttribute("aria-label", running ? "Personas switch between tasks" : "Choose persona");
      } else if (!supported && reason) {
        // Capability gap on an older CLI: keep the control visible but disabled with the standard
        // "Needs a newer Alysis Code CLI" affordance so users learn the feature exists.
        personaButton.hidden = false;
        personaButton.disabled = true;
        personaButton.title = reason;
        personaButton.setAttribute("aria-label", `Personas unavailable: ${reason}`);
      } else {
        // Unsupported with no stated reason, or disabled by the CLI kill switch: hidden entirely.
        personaButton.hidden = true;
      }
    }
    if (personaLabel) {
      personaLabel.textContent = active === "code" ? "Code" : active || "Persona";
    }
    if (personaChip) {
      const activeRow = Array.isArray(personasState?.options)
        ? personasState.options.find((persona) => persona.name === active)
        : null;
      const showChip = supported && enabled && Boolean(active) && active !== "code";
      personaChip.hidden = !showChip;
      if (showChip) {
        const mode = friendlyModeLabel(activeRow?.effectiveMode);
        personaChip.textContent = mode ? `${active} · ${mode}` : active;
        personaChip.setAttribute(
          "aria-label",
          mode ? `Active persona: ${active}, ${mode}` : `Active persona: ${active}`
        );
        personaChip.title = String(activeRow?.description || `Active persona: ${active}`);
      } else {
        personaChip.textContent = "";
      }
    }
    if (personaPaletteOpen) {
      if (!supported || !enabled || running) {
        closePersonaPalette();
      } else {
        renderPersonaPalette();
      }
    }
  }

  /**
   * Models offered for the active connection: the provider's suggested list plus whatever is
   * currently set, so a hand-typed model never disappears from its own picker.
   */
  /**
   * The host already merges the configured model with the matching preset's suggestions into
   * `connection.models` (matched by preset name, else protocol + host). The name-only lookup stays
   * as a fallback so a host that predates the merge still offers something beyond the current model.
   */
  function modelsForActiveProvider(active) {
    const models = [];
    const push = (value) => {
      const model = String(value || "").trim();
      if (model.length > 0 && !models.includes(model)) {
        models.push(model);
      }
    };
    push(active?.model);
    for (const model of active?.models || []) {
      push(model);
    }
    const entry = (modelsState?.providers || []).find((provider) => provider.profileName === active?.profile);
    for (const model of entry?.models || []) {
      push(model);
    }
    return models;
  }

  function paletteRow(title, meta, description, onClick, current) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `command-row${current ? " current" : ""}`;
    button.setAttribute("role", "option");
    button.title = [title, meta, description].filter(Boolean).join("\n");
    button.append(textNode(title, "command-title"), textNode(meta, "command-category"));
    if (description) {
      button.append(textNode(description, "command-description"));
    }
    button.addEventListener("click", onClick);
    return button;
  }

  function requestModels() {
    if (modelsRequested) {
      return;
    }
    modelsRequested = true;
    host.postMessage({ type: "models.refresh" });
  }

  // ---------------------------------------------------------------------------
  // Composer @ mentions.
  // ---------------------------------------------------------------------------

  /** The @token under the caret, or null when the caret is not inside one. */
  function mentionQueryAtCaret() {
    if (!taskInput) {
      return null;
    }
    const caret = taskInput.selectionStart ?? 0;
    if (caret !== (taskInput.selectionEnd ?? 0)) {
      return null;
    }
    const before = taskInput.value.slice(0, caret);
    const match = /(^|\s)@([^\s@]*)$/.exec(before);
    if (!match) {
      return null;
    }
    return { query: match[2], start: caret - match[2].length - 1, end: caret };
  }

  function scheduleMentionSearch() {
    const mention = mentionQueryAtCaret();
    if (!mention || (commandPalette && !commandPalette.hidden)) {
      hideMentionPalette();
      return;
    }
    mentionRange = mention;
    if (mentionTimer) {
      clearTimeout(mentionTimer);
    }
    // Debounced so a fast typist does not queue one workspace search per keystroke.
    mentionTimer = setTimeout(() => {
      mentionTimer = 0;
      mentionToken += 1;
      host.postMessage({ type: "mention.search", query: mention.query, token: mentionToken });
    }, 120);
  }

  function applyMentionResults(token, results) {
    if (token !== mentionToken) {
      return;
    }
    mentionResults = Array.isArray(results) ? results : [];
    renderMentionPalette();
  }

  function renderMentionPalette() {
    if (!mentionPalette || !mentionList) {
      return;
    }
    if (!mentionRange || mentionResults.length === 0) {
      hideMentionPalette();
      return;
    }
    if (mentionHint) {
      mentionHint.textContent = `${mentionResults.length} match${mentionResults.length === 1 ? "" : "es"}`;
    }
    mentionList.replaceChildren(...mentionResults.map((result) =>
      paletteRow(result.label, result.kind === "folder" ? "Folder" : "", result.detail, () => {
        insertMention(result.insert);
      }, false)
    ));
    mentionPalette.hidden = false;
    paletteSelection = 0;
    updateCommandSelection(paletteRows(mentionPalette));
  }

  function hideMentionPalette() {
    if (mentionPalette) {
      mentionPalette.hidden = true;
    }
    mentionResults = [];
    mentionRange = null;
  }

  function insertMention(insert) {
    if (!taskInput || !mentionRange) {
      return;
    }
    const replacement = `@${insert} `;
    taskInput.setRangeText(replacement, mentionRange.start, mentionRange.end, "end");
    hideMentionPalette();
    taskInput.focus();
    autoGrowComposer();
    persistDraft();
    updateSubmitState();
  }

  function showForgeTab(nextTab) {
    if (!["plan", "changes", "files", "activity"].includes(nextTab)) {
      return;
    }
    forgeTab = nextTab;
    for (const button of document.querySelectorAll("[data-forge-tab]")) {
      const tab = button.getAttribute("data-forge-tab") || "plan";
      const selected = tab === nextTab;
      button.id = `forgeTab-${tab}`;
      button.setAttribute("aria-controls", `forge${tab[0].toUpperCase()}${tab.slice(1)}Panel`);
      button.classList.toggle("selected", selected);
      button.setAttribute("aria-selected", selected ? "true" : "false");
      button.setAttribute("tabindex", selected ? "0" : "-1");
    }
    for (const panel of document.querySelectorAll("[data-forge-panel]")) {
      if (panel instanceof HTMLElement) {
        panel.hidden = panel.getAttribute("data-forge-panel") !== nextTab;
        panel.setAttribute("role", "tabpanel");
        panel.setAttribute("aria-labelledby", `forgeTab-${panel.getAttribute("data-forge-panel")}`);
        panel.tabIndex = 0;
      }
    }
    persistDraft();
  }

  function postCockpitAction(action, source) {
    let message = null;
    switch (action) {
      case "forge.executePreview": message = { type: action, auto: true }; break;
      case "forge.executeReview":
      case "forge.status.refresh":
      case "forge.assets.refresh":
      case "swarm.start":
      case "swarm.cancel":
      case "swarm.review": message = { type: action }; break;
      case "swarm.recovery.refresh": message = { type: action }; break;
      case "swarm.recovery.resume":
      case "swarm.recovery.dismiss": message = {
        type: action,
        jobId: source?.dataset.jobId || "",
        revision: Number(source?.dataset.revision)
      }; break;
      case "forge.diff.open": message = { type: action, diffId: source?.dataset.diffId || "" }; break;
      case "forge.review": message = { type: action, taskId: source?.dataset.taskId || undefined }; break;
      case "forge.assets.open": message = { type: action, assetId: source?.dataset.assetId || "" }; break;
      case "forge.artifact.open": message = { type: action, sessionId: source?.dataset.sessionId || "", artifactId: source?.dataset.artifactId || "" }; break;
      case "forge.approval": message = { type: action, sessionId: source?.dataset.sessionId || "", approvalId: source?.dataset.approvalId || "", decision: source?.dataset.decision || "deny" }; break;
      case "swarm.apply":
      case "swarm.discard": message = { type: action, taskId: source?.dataset.taskId || "" }; break;
      // A swarm diff is stored as a Forge artifact, so reviewing it reuses the artifact opener.
      case "swarm.diff.open": message = {
        type: "forge.artifact.open",
        sessionId: source?.dataset.sessionId || "",
        artifactId: source?.dataset.artifactId || ""
      }; break;
    }
    if (message) {
      host.postMessage({ type: "cockpit", message });
    }
  }

  function actionButton(label, action) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.dataset.dynamicCockpitAction = action;
    return button;
  }

  function forgeApprovalButton(label, approval, decision) {
    const button = actionButton(label, "forge.approval");
    button.dataset.sessionId = approval.sessionId;
    button.dataset.approvalId = approval.approvalId;
    button.dataset.decision = decision;
    return button;
  }

  function sectionLabel(value) {
    const heading = document.createElement("h2");
    heading.className = "subsection-label";
    heading.textContent = value;
    return heading;
  }

  function emptyPanel(value) {
    const node = document.createElement("p");
    node.className = "empty-panel";
    node.textContent = value;
    return node;
  }

  function metaRow(label, value, code) {
    const row = document.createElement("div");
    row.className = "forge-meta-row";
    row.append(textNode(label, "forge-meta-label"));
    const content = textNode(value, code ? "forge-meta-value code" : "forge-meta-value");
    row.append(content);
    return row;
  }

  function metaList(label, values, code) {
    const root = document.createElement("div");
    root.className = "forge-meta-list";
    root.append(textNode(label, "forge-meta-label"));
    const list = document.createElement("ul");
    for (const value of values) {
      const li = document.createElement("li");
      li.className = code ? "code" : "";
      li.textContent = String(value);
      list.append(li);
    }
    root.append(list);
    return root;
  }

  function activityRow(title, detail, severity) {
    const row = document.createElement("div");
    row.className = `activity-row ${severity === "error" ? "error" : severity === "warning" ? "warning" : "info"}`;
    row.append(textNode(title, "activity-title"), textNode(detail, "activity-detail"));
    return row;
  }

  function countForgeFiles(forge) {
    return (forge?.assets?.length || 0) + (forge?.artifacts || []).reduce((total, group) => total + (group.artifacts?.length || 0), 0);
  }

  function formatBytes(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(value < 10240 ? 1 : 0)} KB`;
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  }

  function shortValue(value) {
    const text = String(value || "");
    return text.length > 14 ? `${text.slice(0, 12)}…` : text;
  }

  function safeJson(value) {
    if (value === null || value === undefined) return "No additional details.";
    if (typeof value === "string") return value;
    try { return JSON.stringify(value, null, 2); } catch { return "Result available."; }
  }

  // ---------------------------------------------------------------------------
  // Markdown. Every node is built with createElement + textContent. No raw-HTML
  // sink and no dynamic code evaluation appears anywhere in this file, and
  // test/startViewSecurity.test.ts asserts it stays that way.
  // ---------------------------------------------------------------------------

  function renderRichText(container, value) {
    renderMarkdownBlocks(container, String(value || "").replace(/\r\n/g, "\n").split("\n"));
  }

  function renderMarkdownBlocks(container, lines) {
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (line.trim() === "") {
        index += 1;
        continue;
      }
      const fence = /^\s*(```|~~~)\s*([A-Za-z0-9_+#.-]*)\s*$/.exec(line);
      if (fence) {
        const marker = fence[1];
        const body = [];
        index += 1;
        while (index < lines.length && !new RegExp(`^\\s*${marker}\\s*$`).test(lines[index])) {
          body.push(lines[index]);
          index += 1;
        }
        index += 1;
        container.append(codeBlock(body.join("\n"), fence[2] || ""));
        continue;
      }
      if (/^\s{0,3}([-*_])\s*(\1\s*){2,}$/.test(line)) {
        container.append(document.createElement("hr"));
        index += 1;
        continue;
      }
      const heading = /^\s{0,3}(#{1,6})\s+(.*)$/.exec(line);
      if (heading) {
        const level = Math.min(5, heading[1].length + 2);
        const node = document.createElement(`h${level}`);
        appendInlineText(node, heading[2].replace(/\s+#+\s*$/, ""));
        container.append(node);
        index += 1;
        continue;
      }
      if (/^\s{0,3}>/.test(line)) {
        const quoted = [];
        while (index < lines.length && /^\s{0,3}>/.test(lines[index])) {
          quoted.push(lines[index].replace(/^\s{0,3}>\s?/, ""));
          index += 1;
        }
        const quote = document.createElement("blockquote");
        renderMarkdownBlocks(quote, quoted);
        container.append(quote);
        continue;
      }
      if (isTableStart(lines, index)) {
        index = appendTable(container, lines, index);
        continue;
      }
      if (listMatch(line)) {
        index = appendList(container, lines, index, indentWidth(line));
        continue;
      }
      const paragraph = [];
      while (index < lines.length
        && lines[index].trim() !== ""
        && !listMatch(lines[index])
        && !/^\s*(```|~~~)/.test(lines[index])
        && !/^\s{0,3}>/.test(lines[index])
        && !/^\s{0,3}#{1,6}\s+/.test(lines[index])
        && !isTableStart(lines, index)) {
        paragraph.push(lines[index]);
        index += 1;
      }
      if (paragraph.length > 0) {
        const node = document.createElement("p");
        appendInlineText(node, paragraph.join("\n"));
        container.append(node);
      } else {
        index += 1;
      }
    }
  }

  function listMatch(line) {
    return /^(\s*)(?:[-*+]|\d{1,9}[.)])\s+(.*)$/.exec(line || "");
  }

  function indentWidth(line) {
    return (/^\s*/.exec(line)?.[0] || "").replace(/\t/g, "    ").length;
  }

  /** Consume one list (and any deeper nested lists) starting at `start`, returning the next index. */
  function appendList(container, lines, start, indent) {
    const first = listMatch(lines[start]);
    const ordered = /^\s*\d/.test(lines[start]);
    const list = document.createElement(ordered ? "ol" : "ul");
    let index = start;
    let item = null;
    while (index < lines.length) {
      const line = lines[index];
      if (line.trim() === "") {
        const next = lines[index + 1];
        if (!next || !listMatch(next) || indentWidth(next) < indent) {
          break;
        }
        index += 1;
        continue;
      }
      const match = listMatch(line);
      const width = indentWidth(line);
      if (match && width >= indent + 2 && item) {
        index = appendList(item, lines, index, width);
        continue;
      }
      if (!match || width < indent) {
        break;
      }
      item = document.createElement("li");
      appendInlineText(item, match[2]);
      list.append(item);
      index += 1;
    }
    if (list.childElementCount === 0 && first) {
      const only = document.createElement("li");
      appendInlineText(only, first[2]);
      list.append(only);
      index = start + 1;
    }
    container.append(list);
    return index;
  }

  function isTableStart(lines, index) {
    const header = lines[index] || "";
    const divider = lines[index + 1] || "";
    return header.includes("|")
      && /^\s*\|?[\s:-]*-[\s:|-]*$/.test(divider)
      && divider.includes("-")
      && divider.includes("|");
  }

  function appendTable(container, lines, start) {
    const table = document.createElement("table");
    table.className = "md-table";
    const head = document.createElement("thead");
    head.append(tableRow(lines[start], "th"));
    table.append(head);
    const body = document.createElement("tbody");
    let index = start + 2;
    while (index < lines.length && lines[index].includes("|") && lines[index].trim() !== "") {
      body.append(tableRow(lines[index], "td"));
      index += 1;
    }
    table.append(body);
    container.append(table);
    return index;
  }

  function tableRow(line, cellTag) {
    const row = document.createElement("tr");
    const cells = String(line).trim().replace(/^\|/, "").replace(/\|$/, "").split("|");
    for (const cell of cells) {
      const node = document.createElement(cellTag);
      appendInlineText(node, cell.trim());
      row.append(node);
    }
    return row;
  }

  /**
   * A code block with the affordances a developer expects: copy, insert at the cursor, and open a
   * diff against the active file. Highlighting is a local, dependency-free tokenizer — the CSP
   * forbids remote scripts and this extension ships zero runtime dependencies.
   */
  function codeBlock(code, language) {
    const normalized = String(code).replace(/\n$/, "");
    const key = normalizeLanguage(language);
    const wrapper = document.createElement("div");
    wrapper.className = "code-block";
    if (language) {
      wrapper.dataset.language = language.slice(0, 24);
    }

    const head = document.createElement("div");
    head.className = "code-block-head";
    head.append(textNode(languageLabel(language, key), "code-block-language"));
    const actions = document.createElement("div");
    actions.className = "code-block-actions";
    actions.append(
      codeActionButton("Copy", "copy", () => {
        host.postMessage({ type: "clipboard.copy", text: normalized });
      }, true),
      codeActionButton("Insert", "insert", () => {
        host.postMessage({ type: "editor.insert", text: normalized });
      }),
      codeActionButton("Apply", "diff", () => {
        host.postMessage({ type: "editor.applyCode", text: normalized, language: key });
      })
    );
    head.append(actions);
    wrapper.append(head);

    const pre = document.createElement("pre");
    if (language) {
      pre.dataset.language = language.slice(0, 24);
    }
    const element = document.createElement("code");
    highlightInto(element, normalized, key);
    pre.append(element);
    wrapper.append(pre);
    return wrapper;
  }

  function codeActionButton(label, icon, onClick, confirms) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "code-action";
    button.title = label === "Insert"
      ? "Insert at the cursor in the active editor"
      : label === "Apply"
        ? "Open this code as a diff against the active file"
        : "Copy this code";
    button.append(iconElement(icon, "code-action-icon"), textNode(label, "code-action-label"));
    button.addEventListener("click", () => {
      onClick();
      if (confirms) {
        const original = button.querySelector(".code-action-label");
        if (original) {
          original.textContent = "Copied";
          setTimeout(() => {
            original.textContent = label;
          }, 1400);
        }
      }
    });
    return button;
  }

  function appendInlineText(container, text) {
    const tokens = String(text).split(/(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|~~[^~\n]+~~|\*[^*\n]+\*|\[[^\]\n]*\]\([^)\s]+\)|https?:\/\/[^\s<>()]+)/g);
    for (const token of tokens) {
      if (!token) {
        continue;
      }
      if (token.startsWith("`") && token.endsWith("`") && token.length > 2) {
        const value = token.slice(1, -1);
        const mention = fileMentionButton(value);
        container.append(mention || codeSpan(value));
        continue;
      }
      if ((token.startsWith("**") && token.endsWith("**")) || (token.startsWith("__") && token.endsWith("__"))) {
        const strong = document.createElement("strong");
        strong.textContent = token.slice(2, -2);
        container.append(strong);
        continue;
      }
      if (token.startsWith("~~") && token.endsWith("~~") && token.length > 4) {
        const strike = document.createElement("del");
        strike.textContent = token.slice(2, -2);
        container.append(strike);
        continue;
      }
      if (token.startsWith("*") && token.endsWith("*") && token.length > 2) {
        const emphasis = document.createElement("em");
        emphasis.textContent = token.slice(1, -1);
        container.append(emphasis);
        continue;
      }
      const link = /^\[([^\]]*)\]\(([^)\s]+)\)$/.exec(token);
      if (link) {
        container.append(linkNode(link[2], link[1] || link[2]));
        continue;
      }
      if (/^https?:\/\//i.test(token)) {
        container.append(linkNode(token, token));
        continue;
      }
      appendPlainText(container, token);
    }
  }

  function codeSpan(value) {
    const code = document.createElement("code");
    code.textContent = value;
    return code;
  }

  /**
   * Only https links become anchors, and the click is routed through the host rather than the
   * href. command: URLs are never rendered as links — they would run extension commands.
   */
  function linkNode(url, label) {
    const value = String(url);
    if (!/^https:\/\//i.test(value)) {
      return document.createTextNode(label === value ? value : `${label} (${value})`);
    }
    const anchor = document.createElement("a");
    anchor.className = "md-link";
    anchor.href = value;
    anchor.title = value;
    anchor.textContent = label;
    anchor.addEventListener("click", (event) => {
      event.preventDefault();
      host.postMessage({ type: "open.external", url: value });
    });
    return anchor;
  }

  /** Paths the agent names become one-click openers; everything else stays plain text. */
  function appendPlainText(container, text) {
    const pattern = /(?:\.{0,2}\/)?[\w.@-]+(?:\/[\w.@-]+)+\.[A-Za-z][\w]{0,9}(?::\d{1,6})?|\b[\w-]+\.(?:ts|tsx|js|jsx|py|rs|go|java|json|md|css|html|yml|yaml|toml|sh)(?::\d{1,6})?\b/g;
    let cursor = 0;
    let match = pattern.exec(text);
    while (match) {
      if (match.index > cursor) {
        container.append(document.createTextNode(text.slice(cursor, match.index)));
      }
      const mention = fileMentionButton(match[0]);
      container.append(mention || document.createTextNode(match[0]));
      cursor = match.index + match[0].length;
      match = pattern.exec(text);
    }
    if (cursor < text.length) {
      container.append(document.createTextNode(text.slice(cursor)));
    }
  }

  function fileMentionButton(value) {
    const candidate = String(value).trim();
    if (!/^(?:\.{0,2}\/)?[\w.@/-]+\.[A-Za-z][\w]{0,9}(?::\d{1,6})?$/.test(candidate) || candidate.length > 240) {
      return null;
    }
    const button = document.createElement("button");
    button.type = "button";
    button.className = "file-mention";
    button.textContent = candidate;
    button.title = `Open ${candidate}`;
    button.addEventListener("click", () => {
      host.postMessage({ type: "open.file", path: candidate });
    });
    return button;
  }

  // ---------------------------------------------------------------------------
  // Syntax highlighting: a small, linear tokenizer. No dependency, no remote
  // asset, no inline script — just spans coloured from VS Code theme tokens.
  // ---------------------------------------------------------------------------

  const LANGUAGE_ALIASES = {
    js: "js", jsx: "js", javascript: "js", mjs: "js", cjs: "js", ts: "js", tsx: "js", typescript: "js",
    py: "python", python: "python", python3: "python",
    json: "json", jsonc: "json",
    sh: "shell", bash: "shell", zsh: "shell", shell: "shell", console: "shell", ps1: "shell", powershell: "shell",
    html: "markup", xml: "markup", svg: "markup", vue: "markup", svelte: "markup",
    css: "css", scss: "css", less: "css",
    go: "go", golang: "go",
    rs: "rust", rust: "rust",
    java: "java", kt: "java", kotlin: "java", cs: "java", csharp: "java",
    c: "clike", h: "clike", cpp: "clike", "c++": "clike", cc: "clike", hpp: "clike", cxx: "clike"
  };

  const LANGUAGE_LABELS = {
    js: "JavaScript", python: "Python", json: "JSON", shell: "Shell", markup: "HTML",
    css: "CSS", go: "Go", rust: "Rust", java: "Java", clike: "C"
  };

  /** What the author wrote wins over the tokenizer family: `ts` is TypeScript, not JavaScript. */
  const WRITTEN_LANGUAGE_LABELS = {
    ts: "TypeScript", typescript: "TypeScript", tsx: "TSX", jsx: "JSX", mjs: "JavaScript", cjs: "JavaScript",
    py: "Python", python3: "Python", jsonc: "JSON", sh: "Shell", bash: "Bash", zsh: "Zsh", console: "Console",
    ps1: "PowerShell", powershell: "PowerShell", xml: "XML", svg: "SVG", vue: "Vue", svelte: "Svelte",
    scss: "SCSS", less: "Less", golang: "Go", rs: "Rust", kt: "Kotlin", kotlin: "Kotlin", cs: "C#",
    csharp: "C#", cpp: "C++", "c++": "C++", cc: "C++", hpp: "C++", cxx: "C++", h: "C"
  };

  const KEYWORDS = {
    js: "abstract as async await break case catch class const continue debugger declare default delete do else enum export extends finally for from function get if implements import in instanceof interface let new of private protected public readonly return satisfies set static super switch this throw try type typeof var void while yield true false null undefined",
    python: "and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield True False None self",
    json: "true false null",
    shell: "if then else elif fi for while do done case esac function return export local readonly set unset source echo cd exit trap shift",
    go: "break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var true false nil",
    rust: "as async await break const continue crate dyn else enum extern fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait type unsafe use where while true false",
    java: "abstract assert boolean break byte case catch char class const continue default do double else enum extends final finally float for if implements import instanceof int interface long native new package private protected public return short static super switch synchronized this throw throws try var void volatile while true false null",
    clike: "auto break case char const continue default do double else enum extern float for goto if inline int long register return short signed sizeof static struct switch typedef union unsigned void volatile while bool class namespace new delete template typename public private protected virtual true false nullptr NULL"
  };

  const KEYWORD_SETS = new Map();

  function keywordSet(key) {
    if (!KEYWORD_SETS.has(key)) {
      KEYWORD_SETS.set(key, new Set(String(KEYWORDS[key] || "").split(/\s+/).filter(Boolean)));
    }
    return KEYWORD_SETS.get(key);
  }

  function normalizeLanguage(language) {
    return LANGUAGE_ALIASES[String(language || "").trim().toLowerCase()] || "";
  }

  function languageLabel(raw, key) {
    const trimmed = String(raw || "").trim();
    return WRITTEN_LANGUAGE_LABELS[trimmed.toLowerCase()]
      || LANGUAGE_LABELS[key]
      || (trimmed ? trimmed.slice(0, 24) : "Code");
  }

  function highlightInto(element, code, key) {
    if (!key || code.length > 40_000) {
      element.textContent = code;
      return;
    }
    const tokens = key === "markup"
      ? tokenizeMarkup(code)
      : key === "css"
        ? tokenizeCss(code)
        : tokenizeCode(code, key);
    for (const token of tokens) {
      if (!token.type) {
        element.append(document.createTextNode(token.value));
        continue;
      }
      const span = document.createElement("span");
      span.className = `tok-${token.type}`;
      span.textContent = token.value;
      element.append(span);
    }
  }

  /** Single left-to-right pass: no backtracking, so a pathological block cannot hang the webview. */
  function tokenizeCode(code, key) {
    const keywords = keywordSet(key);
    const lineComment = key === "python" || key === "shell" ? "#" : "//";
    const supportsBlockComment = key !== "python" && key !== "shell" && key !== "json";
    const tokens = [];
    let index = 0;
    let plain = "";
    const flush = () => {
      if (plain) {
        tokens.push({ type: "", value: plain });
        plain = "";
      }
    };
    while (index < code.length) {
      const rest = code.slice(index);
      if (rest.startsWith(lineComment)) {
        const end = code.indexOf("\n", index);
        const stop = end === -1 ? code.length : end;
        flush();
        tokens.push({ type: "comment", value: code.slice(index, stop) });
        index = stop;
        continue;
      }
      if (supportsBlockComment && rest.startsWith("/*")) {
        const end = code.indexOf("*/", index + 2);
        const stop = end === -1 ? code.length : end + 2;
        flush();
        tokens.push({ type: "comment", value: code.slice(index, stop) });
        index = stop;
        continue;
      }
      const quote = code[index];
      if (quote === '"' || quote === "'" || quote === "`") {
        const triple = key === "python" && rest.startsWith(quote.repeat(3));
        const end = triple
          ? closingIndex(code, index + 3, quote.repeat(3))
          : closingQuote(code, index + 1, quote);
        flush();
        tokens.push({ type: "string", value: code.slice(index, end) });
        index = end;
        continue;
      }
      if (/[0-9]/.test(quote) && !/[\w$]/.test(code[index - 1] || "")) {
        const match = /^0[xXbBoO][0-9a-fA-F_]+|^\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?/.exec(rest);
        if (match) {
          flush();
          tokens.push({ type: "number", value: match[0] });
          index += match[0].length;
          continue;
        }
      }
      const word = /^[A-Za-z_$][\w$]*/.exec(rest);
      if (word) {
        flush();
        const value = word[0];
        const next = code.slice(index + value.length).match(/^\s*\(/);
        if (keywords.has(value)) {
          tokens.push({ type: "keyword", value });
        } else if (next) {
          tokens.push({ type: "function", value });
        } else if (/^[A-Z]/.test(value)) {
          tokens.push({ type: "type", value });
        } else {
          tokens.push({ type: "", value });
        }
        index += value.length;
        continue;
      }
      plain += quote;
      index += 1;
    }
    flush();
    return tokens;
  }

  function closingQuote(code, from, quote) {
    let index = from;
    while (index < code.length) {
      const character = code[index];
      if (character === "\\") {
        index += 2;
        continue;
      }
      if (character === quote) {
        return index + 1;
      }
      if (character === "\n" && quote !== "`") {
        return index;
      }
      index += 1;
    }
    return code.length;
  }

  function closingIndex(code, from, marker) {
    const found = code.indexOf(marker, from);
    return found === -1 ? code.length : found + marker.length;
  }

  function tokenizeMarkup(code) {
    const tokens = [];
    let index = 0;
    while (index < code.length) {
      const open = code.indexOf("<", index);
      if (open === -1) {
        tokens.push({ type: "", value: code.slice(index) });
        break;
      }
      if (open > index) {
        tokens.push({ type: "", value: code.slice(index, open) });
      }
      if (code.startsWith("<!--", open)) {
        const end = code.indexOf("-->", open);
        const stop = end === -1 ? code.length : end + 3;
        tokens.push({ type: "comment", value: code.slice(open, stop) });
        index = stop;
        continue;
      }
      const close = code.indexOf(">", open);
      const stop = close === -1 ? code.length : close + 1;
      const tag = code.slice(open, stop);
      const name = /^<\/?\s*([A-Za-z][\w:-]*)/.exec(tag);
      if (name) {
        tokens.push({ type: "tag", value: tag.slice(0, name[0].length) });
        const remainder = tag.slice(name[0].length);
        for (const part of remainder.split(/("[^"]*"|'[^']*')/)) {
          if (!part) {
            continue;
          }
          tokens.push({ type: /^["']/.test(part) ? "string" : "attr", value: part });
        }
      } else {
        tokens.push({ type: "tag", value: tag });
      }
      index = stop;
    }
    return tokens;
  }

  function tokenizeCss(code) {
    const tokens = [];
    let index = 0;
    let plain = "";
    const flush = () => {
      if (plain) {
        tokens.push({ type: "", value: plain });
        plain = "";
      }
    };
    while (index < code.length) {
      if (code.startsWith("/*", index)) {
        const end = code.indexOf("*/", index + 2);
        const stop = end === -1 ? code.length : end + 2;
        flush();
        tokens.push({ type: "comment", value: code.slice(index, stop) });
        index = stop;
        continue;
      }
      const character = code[index];
      if (character === '"' || character === "'") {
        const end = closingQuote(code, index + 1, character);
        flush();
        tokens.push({ type: "string", value: code.slice(index, end) });
        index = end;
        continue;
      }
      const property = /^\s*(--)?[a-zA-Z-]+\s*:/.exec(code.slice(index));
      if (property && /[{;\n]/.test(code[index - 1] || "\n")) {
        flush();
        tokens.push({ type: "attr", value: property[0] });
        index += property[0].length;
        continue;
      }
      const number = /^-?\d[\d.]*(?:px|rem|em|%|vh|vw|s|ms|deg)?/.exec(code.slice(index));
      if (number && /[\s:(,]/.test(code[index - 1] || " ")) {
        flush();
        tokens.push({ type: "number", value: number[0] });
        index += number[0].length;
        continue;
      }
      plain += character;
      index += 1;
    }
    flush();
    return tokens;
  }

  function renderRecentTasks(tasks) {
    const recent = Array.isArray(tasks) ? tasks : [];
    const signature = JSON.stringify([recent, historyQuery, historyCurrentOnly]);
    if (signature === recentTasksSignature) return;
    recentTasksSignature = signature;
    if (recentBlock) {
      recentBlock.hidden = recent.length === 0;
    }
    if (recentCards) {
      recentCards.replaceChildren(...recent.slice(0, 3).map((task) => taskCard(task, true)));
    }
    if (historyList) {
      const filtered = recent.filter((task) => (!historyCurrentOnly || task.current) && `${task.title || ""} ${task.detail || ""}`.toLocaleLowerCase().includes(historyQuery));
      setText("historyCount", historyQuery || historyCurrentOnly ? `${filtered.length} of ${recent.length} tasks` : `${recent.length} ${recent.length === 1 ? "task" : "tasks"}`);
      if (filtered.length === 0) {
        const empty = document.createElement("div");
        empty.className = "empty-history";
        empty.append(iconElement("history", "empty-icon"), textNode(recent.length ? "No matching tasks" : "Your next task starts here", "empty-title"), textNode(recent.length ? "Try another search or turn off the current-task filter." : "Tasks from this engine session will appear here as you work.", "empty-detail"));
        const reset = document.createElement("button");
        reset.type = "button";
        reset.className = "secondary-action";
        reset.textContent = recent.length ? "Clear filters" : "Start a task";
        reset.addEventListener("click", () => {
          if (!recent.length) { showSurface("task"); return; }
          historyQuery = "";
          historyCurrentOnly = false;
          if (historySearch) historySearch.value = "";
          historyCurrent?.setAttribute("aria-pressed", "false");
          renderRecentTasks(currentState?.recentTasks);
          historySearch?.focus();
        });
        empty.append(reset);
        historyList.replaceChildren(empty);
      } else {
        const focus = document.activeElement instanceof HTMLElement ? document.activeElement.dataset.sessionId : null;
        historyList.replaceChildren(...filtered.map((task) => taskCard(task, false)));
        if (focus) /** @type {HTMLElement | null} */ (historyList.querySelector(`[data-session-id="${cssEscape(focus)}"]`))?.focus();
      }
    }
  }

  function taskCard(task, compact) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.sessionId = String(task.sessionId);
    button.title = `${task.title || "Previous task"}\n${task.detail || "Ready to continue"}`;
    if (task.current) button.setAttribute("aria-current", "true");
    button.className = `task-card${task.current ? " current" : ""}${compact ? " compact" : ""}`;
    button.append(
      textNode(task.title || "Previous task", "task-title"),
      textNode(task.detail || "Ready to continue", "task-detail"),
      textNode(task.current ? "Open" : task.canResume ? "Resume" : "Details", "task-action")
    );
    button.addEventListener("click", () => {
      if (task.current) {
        showSurface("task");
      } else {
        host.postMessage({ type: "session", action: task.canResume ? "resume" : "show", sessionId: task.sessionId });
      }
    });
    return button;
  }

  function renderSettings(state) {
    if (Array.isArray(state.commands)) {
      for (const button of document.querySelectorAll("[data-command]")) {
        if (!(button instanceof HTMLButtonElement)) continue;
        const command = state.commands.find((entry) => entry.command === button.getAttribute("data-command"));
        button.disabled = !command?.available;
        button.title = command?.available ? command.description || "" : command?.unavailableReason || "This action is unavailable in the connected agent.";
      }
    }
    setText("providerSettingsDetail", state.provider?.detail || "Choose a provider");
    setText("workspaceSettingsDetail", state.workspace?.detail || "Check folder access");
    setText("engineSettingsDetail", state.engine?.detail || "Check the local connection");
    setTone("providerSettings", state.provider?.tone);
    setTone("workspaceSettings", state.workspace?.tone);
    setTone("engineSettings", state.engine?.tone);
    // Command gates above still apply to newly rendered buttons. Only the static
    // settings rows can reuse filtering; search input also filters them directly.
    const signature = JSON.stringify([state.commands, state.provider, state.workspace, state.engine, state.forgeEnabled]);
    if (signature !== settingsFilterSignature) {
      settingsFilterSignature = signature;
      filterSettings();
    }
  }

  function filterSettings() {
    const query = settingsSearch?.value.trim().toLocaleLowerCase() || "";
    const terms = query.split(/\s+/).filter(Boolean);
    let matches = 0;
    for (const group of document.querySelectorAll(".settings-group")) {
      const heading = group.querySelector("h2")?.textContent || "";
      let groupMatches = 0;
      for (const row of group.querySelectorAll(".settings-row")) {
        const text = `${heading} ${row.textContent}`.toLocaleLowerCase();
        const matched = terms.every((term) => text.includes(term));
        // Filtering must not override a capability gate applied by the host.
        row.classList.toggle("settings-filtered", !matched);
        if (matched && !row.hasAttribute("hidden")) groupMatches++;
      }
      group.classList.toggle("settings-filtered", groupMatches === 0);
      matches += groupMatches;
    }
    const status = document.getElementById("settingsSearchStatus");
    if (status) {
      status.hidden = !query;
      status.textContent = query ? `${matches} matching ${matches === 1 ? "setting" : "settings"}` : "";
    }
    const empty = document.getElementById("settingsEmpty");
    if (empty) empty.hidden = !query || matches > 0;
  }

  /**
   * Everything blocking a run, pinned directly above the composer so a broken setup can never look
   * healthy. One row per blocker with its own recovery buttons; hidden entirely when nothing blocks.
   */
  /**
   * Blockers the welcome hero already explains are not repeated in the strip: on the empty start
   * surface the hero shows the primary blocker and the strip carries only the rest; once a
   * conversation is open the strip shows everything, because the hero is no longer visible.
   */
  function renderReadinessStrip(readiness, welcomeVisible) {
    if (!readinessStrip) {
      return;
    }
    const all = readiness?.ok === true || !Array.isArray(readiness?.blockers)
      ? []
      : readiness.blockers.slice(0, 3);
    const blockers = welcomeVisible ? all.slice(1) : all;
    if (blockers.length === 0) {
      readinessStrip.replaceChildren();
      readinessStrip.hidden = true;
      return;
    }
    readinessStrip.replaceChildren(...blockers.map((blocker) => {
      const progress = blocker?.progress === true;
      const severity = progress ? "progress" : blocker?.severity === "warning" ? "warning" : "error";
      const row = document.createElement("section");
      row.className = `readiness-row ${severity}`;
      row.dataset.blockerId = String(blocker?.id || "");
      if (progress) {
        row.setAttribute("role", "status");
      }

      const copy = document.createElement("div");
      copy.className = "readiness-copy";
      const title = document.createElement("p");
      title.className = "readiness-title";
      title.append(
        progress ? progressSpinner("readiness-icon") : iconElement(severity === "warning" ? "warning" : "error", "readiness-icon"),
        document.createTextNode(String(blocker?.title || "Alysis Code needs attention"))
      );
      copy.append(title);
      if (blocker?.detail) {
        copy.append(textNode(blocker.detail, "readiness-detail"));
      }
      row.append(copy);

      const actions = Array.isArray(blocker?.actions) ? blocker.actions : [];
      if (actions.length > 0) {
        row.append(actionRow(actions, "readiness-actions"));
      }
      return row;
    }));
    readinessStrip.hidden = false;
  }

  /** A small indeterminate spinner drawn purely in CSS; used wherever a state is "still checking". */
  function progressSpinner(className) {
    const spinner = document.createElement("span");
    spinner.className = className ? `progress-spinner ${className}` : "progress-spinner";
    spinner.setAttribute("aria-hidden", "true");
    return spinner;
  }

  /**
   * The start surface offers setup instead of prompts until a task can run. The hero names the
   * primary blocker and offers THAT blocker's own recovery actions — a missing engine, an untrusted
   * folder, and a missing provider each need a different button, so the copy and the actions must
   * come from the same host-authored blocker. `ready` is only absent on states from older hosts,
   * which are treated as ready so the composer is never hidden by a missing field.
   */
  function renderReadyGate(state) {
    composerReady = state.ready === true;
    const blocker = Array.isArray(state.readiness?.blockers) ? state.readiness.blockers[0] : null;
    const signature = JSON.stringify([composerReady, blocker, state.readyReason]);
    if (signature === readyGateSignature) {
      syncWorkflowContext();
      updateSubmitState();
      return;
    }
    readyGateSignature = signature;
    if (connectFirst) {
      connectFirst.hidden = composerReady;
    }
    const progress = blocker?.progress === true;
    readyGateProgress = progress;
    connectFirst?.classList.toggle("progress", progress);
    const heading = document.getElementById("connectFirstTitle");
    if (heading) {
      heading.replaceChildren(
        ...(progress ? [progressSpinner("connect-first-spinner")] : []),
        document.createTextNode(String(blocker?.title || "Connect a model to start"))
      );
    }
    if (connectFirstReason) {
      connectFirstReason.textContent = String(
        blocker?.detail || state.readyReason || "Choose a provider and model before sending a task."
      );
    }
    const actionsHost = document.getElementById("connectFirstActions");
    if (actionsHost) {
      const actions = Array.isArray(blocker?.actions) ? blocker.actions.filter((action) => action?.label && action?.command) : [];
      if (blocker && actions.length > 0) {
        actionsHost.replaceChildren(...actions.slice(0, 3).map((action, index) => {
          const button = document.createElement("button");
          button.type = "button";
          button.className = action.primary || index === 0 ? "primary-action" : "quiet-link";
          button.textContent = String(action.label);
          button.addEventListener("click", () => {
            const args = Array.isArray(action.args)
              ? action.args.filter((value) => typeof value === "string").slice(0, 4)
              : undefined;
            host.postMessage({ type: "command", command: String(action.command), ...(args && args.length > 0 ? { args } : {}) });
          });
          return button;
        }));
      } else if (blocker && progress) {
        // Nothing to do while a check is in flight; a stray "Choose a provider" here sends people
        // away from a connection that is about to succeed.
        actionsHost.replaceChildren();
      } else {
        const choose = document.createElement("button");
        choose.type = "button";
        choose.className = "primary-action";
        choose.textContent = "Choose a provider";
        choose.addEventListener("click", () => showSurface("models"));
        const check = document.createElement("button");
        check.type = "button";
        check.className = "quiet-link";
        check.textContent = "Check my setup instead";
        check.addEventListener("click", () => host.postMessage({ type: "command", command: "alysis.runDoctor" }));
        actionsHost.replaceChildren(choose, check);
      }
    }
    if (starterPrompts) {
      starterPrompts.hidden = !composerReady;
    }
    syncWorkflowContext();
    updateSubmitState();
  }

  function showSurface(nextSurface) {
    surface = nextSurface;
    for (const button of document.querySelectorAll(".sidebar-top-bar [data-surface]")) {
      if (button.getAttribute("data-surface") === nextSurface) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    }
    closeComposerPalettes();
    closeModelPalette();
    closePersonaPalette();
    for (const candidate of ["task", "forge", "browser", "history", "settings", "activity", "models"]) {
      const node = document.getElementById(`${candidate}Surface`);
      if (node) {
        node.hidden = candidate !== nextSurface;
      }
    }
    if (nextSurface === "models") {
      requestModels();
      setTimeout(() => providerSearch?.focus(), 0);
    }
    if (nextSurface === "browser") {
      updateBrowserControls();
    }
    if (nextSurface === "history") setTimeout(() => historySearch?.focus(), 0);
    if (nextSurface === "settings") setTimeout(() => settingsSearch?.focus(), 0);
    if (nextSurface === "activity" || nextSurface === "browser") {
      const heading = document.querySelector(`#${nextSurface}Surface h1`);
      if (heading instanceof HTMLElement) {
        heading.tabIndex = -1;
        heading.focus();
      }
    }
    const composerVisible = nextSurface === "task" || nextSurface === "forge";
    document.body.classList.toggle("secondary-open", !composerVisible);
    document.body.classList.toggle("forge-open", nextSurface === "forge");
    syncWorkflowContext();
    persistDraft();
    syncModeControl();
    if (composerVisible) {
      autoGrowComposer();
      setTimeout(() => taskInput?.focus(), 0);
    }
  }

  function updateSubmitState() {
    const instruction = taskInput?.value.trim() || "";
    const mode = taskMode?.value || "";
    const tooLong = instruction.length > MAX_TASK_CHARS;
    const blocked = !composerReady || taskPending || forgeRunning || (taskRunning && workflow !== "chat") || tooLong;
    if (composerError) {
      composerError.hidden = !tooLong;
      composerError.textContent = tooLong ? `Your draft is preserved. Shorten it to ${MAX_TASK_CHARS.toLocaleString()} characters before sending.` : "";
    }
    taskInput?.setAttribute("aria-invalid", String(tooLong));
    if (taskForm) {
      taskForm.setAttribute("aria-busy", taskPending || forgeRunning ? "true" : "false");
    }
    if (submitTask) {
      submitTask.disabled = blocked || !instruction || !["readonly", "review", "auto"].includes(mode);
      setText("submitTaskLabel", taskRunning && workflow === "chat" ? "Queue" : "Send");
      submitTask.classList.toggle("queue-task", taskRunning && workflow === "chat");
      submitTask.setAttribute("aria-label", taskRunning && workflow === "chat" ? "Queue follow-up" : "Send task");
      submitTask.title = taskRunning && workflow === "chat" ? "Queue after the current request (Enter)" : "Send (Enter)";
      // While a task runs and nothing is typed, Stop is the only meaningful control: hide Send so
      // the row reads as one clear action instead of a disabled button next to a live one.
      submitTask.hidden = (taskRunning || forgeRunning || taskPending) && !instruction;
    }
    if (cancelTask) {
      const running = taskRunning || forgeRunning;
      const stopping = currentState?.conversation?.jobStatus === "cancellation_requested";
      cancelTask.hidden = !running;
      cancelTask.disabled = stopping;
      setText("cancelTaskLabel", stopping ? "Stopping…" : "Stop");
      cancelTask.title = stopping ? "Stopping at the next safe point" : "Stop (Esc)";
    }
  }

  function createRequestId() {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
      return globalThis.crypto.randomUUID();
    }
    return `submit-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function clearPendingAckTimer() {
    if (taskAckTimer) {
      clearTimeout(taskAckTimer);
      taskAckTimer = 0;
    }
  }

  /**
   * Safety net only. The host always answers a submit with task.result (accepted or not), and a
   * cold start — engine launch, session creation, context collection — routinely takes 20–30 s.
   * Restoring the draft before that answer arrives made the message vanish and reappear, so the
   * timeout is generous and exists solely for a message the host never saw (a reload mid-flight).
   */
  const PENDING_ACK_TIMEOUT_MS = 120_000;

  function armPendingAckTimer() {
    clearPendingAckTimer();
    taskAckTimer = setTimeout(() => {
      taskAckTimer = 0;
      if (!taskPending || !pendingInstruction || !taskInput) {
        return;
      }
      restorePendingTask();
    }, PENDING_ACK_TIMEOUT_MS);
  }

  function restorePendingTask() {
    clearPendingAckTimer();
    if (!taskInput || !pendingInstruction) {
      taskPending = false;
      return;
    }
    const nextDraft = taskInput.value.trim();
    taskInput.value = `${pendingInstruction}${nextDraft ? `\n\n${nextDraft}` : ""}`;
    retryInstruction = pendingInstruction;
    retryRequestId = pendingRequestId;
    retryMode = pendingMode;
    retryWorkflow = pendingWorkflow;
    pendingInstruction = "";
    pendingRequestId = "";
    taskPending = false;
    persistDraft();
    autoGrowComposer();
    updateSubmitState();
    renderConversation(currentState?.conversation || null);
  }

  function acknowledgeAcceptedTask(requestId) {
    if (!requestId) return;
    if (pendingRequestId === requestId) {
      clearPendingAckTimer();
      pendingInstruction = "";
      pendingRequestId = "";
      taskPending = false;
    }
    if (retryRequestId === requestId) {
      if (taskInput && taskInput.value.trim() === retryInstruction) {
        taskInput.value = "";
      } else if (taskInput && taskInput.value.startsWith(`${retryInstruction}\n\n`)) {
        taskInput.value = taskInput.value.slice(retryInstruction.length + 2);
      }
      retryInstruction = "";
      retryRequestId = "";
      retryMode = "review";
      retryWorkflow = "chat";
    }
    persistDraft();
    autoGrowComposer();
    updateSubmitState();
    if (pendingRequestId === "" && conversationItems?.querySelector("[data-item-id^='pending:']")) {
      renderConversation(currentState?.conversation || null);
    }
  }

  function autoGrowComposer() {
    if (!taskInput) {
      return;
    }
    const scrollTop = taskInput.scrollTop;
    const editingEnd = document.activeElement === taskInput
      && taskInput.selectionStart === taskInput.value.length
      && taskInput.selectionEnd === taskInput.value.length;
    taskInput.style.height = "auto";
    // CSS owns the compact minimum and viewport-aware maximum. Measuring the full
    // content lets it grow as needed, then shrink back after sending or deleting.
    taskInput.style.height = `${taskInput.scrollHeight}px`;
    taskInput.scrollTop = editingEnd ? taskInput.scrollHeight : scrollTop;
    sizeComposerPalettes();
  }

  function sizeComposerPalettes() {
    if (!taskForm) return;
    const available = Math.max(0, taskForm.getBoundingClientRect().top - 8);
    taskForm.style.setProperty("--alysis-palette-height", `${available}px`);
  }

  window.addEventListener("resize", autoGrowComposer);

  function persistDraft() {
    host.setState({
      draft: taskInput?.value || "",
      pendingInstruction,
      pendingRequestId,
      pendingMode,
      pendingWorkflow,
      retryInstruction,
      retryRequestId,
      retryMode,
      retryWorkflow,
      mode: taskMode?.value || "review",
      surface,
      workflow,
      forgeTab,
      history
    });
  }

  function approvalButton(label, approvalId, decision, secondary) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = secondary ? "approval-button secondary" : "approval-button";
    button.textContent = label;
    button.setAttribute("data-approval-id", approvalId);
    button.setAttribute("data-approval-decision", decision);
    // Focus survives a re-render: the reconciler restores the element with the same focus key.
    button.setAttribute("data-focus-key", `approval:${approvalId}:${decision}`);
    return button;
  }

  function itemSignature(item) {
    if (item.kind === "work") {
      return `${item.phase || ""}\u0001${item.tools.map((tool) => itemSignature(tool)).join("\u0002")}`;
    }
    return [
      item.kind,
      item.title,
      item.text,
      item.status,
      item.toolName,
      item.toolInput,
      item.semanticActivity,
      item.turnLevel,
      item.approvalId,
      item.allowForSession,
      item.errorKind,
      item.errorTitle,
      item.errorDetail,
      item.retryAfterSeconds,
      item.approval ? JSON.stringify(item.approval) : "",
      Array.isArray(item.errorActions)
        ? item.errorActions.map((action) => `${action?.label}${action?.command}`).join(",")
        : ""
    ].join("\u0001");
  }

  function isCompleteStatus(status) {
    return ["complete", "completed", "ok", "success", "succeeded"].includes(String(status || "").toLowerCase());
  }

  function isActiveStatus(status) {
    return ["running", "pending", "queued", "starting", ""].includes(String(status || "").toLowerCase());
  }

  function normalizedWorkStatus(status) {
    const value = String(status || "").toLowerCase();
    if (["cancelled", "canceled", "stopped"].includes(value)) return "stopped";
    if (value === "ended") return "ended";
    if (["failed", "error"].includes(value)) {
      return "failed";
    }
    return isCompleteStatus(value) ? "complete" : "running";
  }

  function workGroupStatus(tools) {
    if (tools.some((tool) => normalizedWorkStatus(tool.status) === "failed")) {
      return "failed";
    }
    if (tools.some((tool) => normalizedWorkStatus(tool.status) === "running")) {
      return "running";
    }
    if (tools.some((tool) => normalizedWorkStatus(tool.status) === "stopped")) return "stopped";
    if (tools.some((tool) => normalizedWorkStatus(tool.status) === "ended")) return "ended";
    return "complete";
  }

  function workProgressTitle(status, presentation, count) {
    if (status === "stopped") return "Work stopped";
    if (status === "ended") return "Work ended";
    if (status === "failed") {
      return "A step needs attention";
    }
    if (status === "running") {
      return presentation.title;
    }
    return count === 1 ? presentation.completedTitle : `Completed ${count} steps`;
  }

  function stepCountLabel(count) {
    return `${count} ${count === 1 ? "step" : "steps"}`;
  }

  function toolPresentation(tool) {
    if (tool?.semanticActivity === true) {
      const displayTitle = clipText(tool.title || "Working on the task", 120);
      return {
        kind: String(tool.toolName || "work"),
        title: displayTitle,
        completedTitle: displayTitle
      };
    }
    const rawName = String(tool?.toolName || tool?.title || "tool");
    const normalized = rawName.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
    const words = new Set(normalized.split(/\s+/).filter(Boolean));
    const has = (...values) => values.some((value) => words.has(value));

    if (has("subagent", "delegate", "delegating", "worker", "agent")) {
      return { kind: "delegate", title: "Delegating work", completedTitle: "Delegated work" };
    }
    if (has("plan", "planning", "todo")) {
      return { kind: "plan", title: "Planning next steps", completedTitle: "Planned next steps" };
    }
    if ((has("web", "browser") && has("search", "query")) || normalized.includes("search web")) {
      return { kind: "web-search", title: "Searching the web", completedTitle: "Searched the web" };
    }
    if (has("patch", "write", "edit", "replace", "create", "delete", "move", "rename", "apply")) {
      return { kind: "change", title: "Making changes", completedTitle: "Made changes" };
    }
    if (has("shell", "exec", "execute", "command", "terminal", "bash", "powershell", "run")) {
      return { kind: "command", title: "Running a command", completedTitle: "Ran a command" };
    }
    if (has("grep", "rg", "search", "find", "query", "locate")) {
      return { kind: "search", title: "Searching the codebase", completedTitle: "Searched the codebase" };
    }
    if (has("glob", "list", "tree", "explore", "scan", "walk")) {
      return { kind: "explore", title: "Exploring project files", completedTitle: "Explored project files" };
    }
    if (has("fetch", "browser", "url", "http", "request", "download")) {
      return { kind: "web-read", title: "Reading a web page", completedTitle: "Read a web page" };
    }
    if (has("read", "open", "view", "load", "cat")) {
      return { kind: "read", title: "Reading files", completedTitle: "Read files" };
    }
    if (has("test", "check", "lint", "compile", "build", "verify")) {
      return { kind: "check", title: "Checking the work", completedTitle: "Checked the work" };
    }
    return { kind: "work", title: "Working", completedTitle: "Finished a step" };
  }

  function toolContext(tool, kind) {
    if (["command", "delegate", "plan", "check", "work"].includes(kind)) {
      return "";
    }
    const preview = String(tool?.toolInput || "").trim();
    if (!preview) {
      return "";
    }
    const candidate = previewValue(preview, kind);
    if (!candidate) {
      return "";
    }
    if (kind === "web-read" || kind === "web-search") {
      try {
        return new URL(candidate).hostname || clipText(candidate, 72);
      } catch {
        return clipText(candidate, 72);
      }
    }
    if (kind === "search") {
      return `\"${clipText(candidate, 64)}\"`;
    }
    return compactPath(candidate);
  }

  function previewValue(preview, kind) {
    const keys = kind === "search" || kind === "web-search"
      ? ["query", "pattern", "search", "text"]
      : kind === "web-read"
        ? ["url", "uri", "href"]
        : ["path", "file_path", "file", "uri", "target"];
    try {
      const parsed = JSON.parse(preview);
      if (parsed && typeof parsed === "object") {
        for (const key of keys) {
          if (typeof parsed[key] === "string") {
            return parsed[key];
          }
        }
      }
    } catch {
      // Protocol previews are often plain paths rather than JSON.
    }
    for (const key of keys) {
      const match = preview.match(new RegExp(`[\"']${key}[\"']\\s*:\\s*[\"']([^\"']+)[\"']`, "i"));
      if (match) {
        return match[1];
      }
    }
    if (!/[\r\n]/.test(preview) && preview.length <= 240 && !/^[{[]/.test(preview)) {
      return preview.replace(/^[\"']|[\"']$/g, "");
    }
    return "";
  }

  function compactPath(value) {
    const normalized = String(value || "").replaceAll("\\", "/");
    const parts = normalized.split("/").filter(Boolean);
    const compact = parts.length > 2 ? parts.slice(-2).join("/") : normalized;
    return clipText(compact, 72);
  }

  function clipText(value, maxLength) {
    const text = String(value || "").trim();
    return text.length > maxLength ? `${text.slice(0, maxLength - 1)}\u2026` : text;
  }

  function technicalToolDetails(tool) {
    const name = clipText(tool?.toolName || tool?.title || "tool", 160);
    const input = clipText(tool?.toolInput || "", 2_000);
    const result = clipText(tool?.text || "", 2_000);
    const details = [`Tool: ${name}`];
    if (input) {
      details.push(`Input:\n${input}`);
    }
    if (result && result !== input) {
      details.push(`${isCompleteStatus(tool?.status) ? "Result" : "Latest update"}:\n${result}`);
    }
    return details.join("\n\n");
  }

  function workMarker(status) {
    const marker = document.createElement("span");
    marker.className = "work-progress-marker";
    marker.setAttribute("aria-hidden", "true");
    if (status !== "running") {
      marker.append(statusIcon(status === "complete" || status === "failed" ? status : "neutral"));
    }
    return marker;
  }

  function workStepStatus(status) {
    if (status === "stopped") return "Stopped";
    if (status === "ended") return "Ended";
    if (status === "complete") {
      return "Done";
    }
    if (status === "failed") {
      return "Failed";
    }
    return "In progress";
  }

  function textNode(text, className) {
    const node = document.createElement("span");
    node.className = className;
    node.textContent = String(text || "");
    return node;
  }

  // ---------------------------------------------------------------------------
  // Icons. Built with createElementNS so they inherit the theme's foreground
  // colour and scale with the surrounding text — no icon font, no extra package,
  // and nothing that would need a CSP exception.
  // ---------------------------------------------------------------------------

  const ICON_PATHS = {
    send: "M2 2.5 14 8 2 13.5l2-5.5zM4 8h10",
    attachment: "M5.5 8.5 9.8 4.2a2 2 0 0 1 2.8 2.8l-6.2 6.2a3 3 0 0 1-4.2-4.2l6.2-6.2M4.6 9.4l4.5-4.5",
    forge: "M2.5 3.5h4v4h-4zM9.5 8.5h4v4h-4zM4.5 7.5V11h5M6.5 5.5H11v3",
    models: "M8 1.5 14 5 8 8.5 2 5zM2 8l6 3.5L14 8M2 11l6 3.5 6-3.5",
    settings: "M3.5 2v12M8 2v12M12.5 2v12M2 5h3M6.5 10h3M11 6h3",
    "chevron-right": "M6 3.5 10.5 8 6 12.5",
    "chevron-left": "M10 3.5 5.5 8 10 12.5",
    "chevron-down": "M3.5 6 8 10.5 12.5 6",
    "arrow-down": "M8 3v10M4 9l4 4 4-4",
    add: "M8 3.5v9M3.5 8h9",
    mention: "M10.5 6.2a2.8 2.8 0 1 0 0 3.6M10.5 5v4.2a1.8 1.8 0 0 0 2.6 1.4M13.4 11.9A6 6 0 1 1 14 8",
    selection: "M2.5 13.5 3 10.6l7-7 2.4 2.4-7 7zM9.6 4 12 6.4",
    commands: "M9.8 2.5 6.2 13.5M3.5 5.5 1.5 8l2 2.5M12.5 5.5 14.5 8l-2 2.5",
    history: "M8 4.5V8l2.5 1.5M2.6 8a5.4 5.4 0 1 0 1.6-3.8M2.5 3v2.6h2.6",
    copy: "M5.5 5.5h7v7h-7zM3.5 10.5v-7h7",
    code: "M5.5 4 1.5 8l4 4M10.5 4l4 4-4 4M9 2.5l-2 11",
    search: "M7 2.5a4.5 4.5 0 1 0 0 9 4.5 4.5 0 0 0 0-9M10.5 10.5l3 3",
    debug: "M5 6h6v5a3 3 0 0 1-6 0zM6 6V4a2 2 0 0 1 4 0v2M2 7h3M11 7h3M2 10h3M11 10h3M3 14l2.5-2M13 14l-2.5-2M8 7v6",
    shield: "M8 1.5 13 3.5v4c0 3-2 5-5 7-3-2-5-4-5-7v-4zM5.5 7.5l2 2 3-3",
    insert: "M8 3v7M5 7.2 8 10.4l3-3.2M3.5 13h9",
    diff: "M4.5 3v10M11.5 3v10M2.5 6h4M9.5 10h4",
    check: "M3.5 8.5 6.5 11.5 12.5 4.5",
    close: "M4 4l8 8M12 4l-8 8",
    dot: "M8 6.6a1.4 1.4 0 1 0 0 2.8 1.4 1.4 0 0 0 0-2.8",
    circle: "M8 2.8a5.2 5.2 0 1 0 0 10.4 5.2 5.2 0 0 0 0-10.4",
    "circle-slash": "M8 2.8a5.2 5.2 0 1 0 0 10.4 5.2 5.2 0 0 0 0-10.4M4.3 11.7 11.7 4.3",
    warning: "M8 2.6 14.4 13H1.6zM8 6.5v3.2M8 11.2v.1",
    error: "M8 2.8a5.2 5.2 0 1 0 0 10.4 5.2 5.2 0 0 0 0-10.4M8 5.4v3.4M8 10.6v.1",
    fork: "M4 5v5M12 5v1c0 2-8 2-8 4M4 2a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3M12 2a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3M4 10a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3",
    image: "M2.5 3.5h11v9h-11zM2.5 10l3-3 2.5 2.5 2-2 3.5 3.5M6 6.3v.1"
  };

  function iconElement(name, className) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("width", "16");
    svg.setAttribute("height", "16");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    svg.setAttribute("class", className ? `sy-icon ${className}` : "sy-icon");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", ICON_PATHS[name] || ICON_PATHS.circle);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.3");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    svg.append(path);
    return svg;
  }

  /** Fill every static [data-icon] slot in the markup once, at boot. */
  function applyStaticIcons() {
    for (const host of document.querySelectorAll("[data-icon]")) {
      const name = host.getAttribute("data-icon") || "";
      if (!ICON_PATHS[name] || host.querySelector("svg")) {
        continue;
      }
      host.append(iconElement(name, "static-icon"));
    }
  }

  /** Status glyphs as icons, so completed/failed/running read the same as native VS Code UI. */
  function statusIcon(status, className) {
    const name = status === "complete" ? "check" : status === "failed" ? "close" : status === "running" ? "dot" : "circle";
    return iconElement(name, className ? `${className} state-${status || "neutral"}` : `state-${status || "neutral"}`);
  }

  /** A run-state marker for plan, swarm, and activity rows. The words beside it carry the meaning. */
  function stateMarker(status, className) {
    const value = String(status || "").toLowerCase();
    const state = ["completed", "complete", "done", "passed", "merged", "kept", "ok", "success"].includes(value)
      ? "complete"
      : ["failed", "error", "blocked"].includes(value)
        ? "failed"
        : ["running", "working", "approval", "pending", "queued", "starting"].includes(value)
          ? "running"
          : "neutral";
    const host = document.createElement("span");
    host.className = className;
    host.setAttribute("aria-hidden", "true");
    host.append(statusIcon(state));
    return host;
  }

  function workStepMarker(status) {
    if (status === "completed_unverified") return stateMarker("neutral", "work-step-glyph");
    if (status === "stopped" || status === "ended") return stateMarker("neutral", "work-step-glyph");
    if (status !== "complete" && status !== "failed") {
      // A static dot marks the active step; only the disclosure's main indicator animates.
      const running = document.createElement("span");
      running.className = "work-step-glyph";
      running.setAttribute("aria-hidden", "true");
      return running;
    }
    return stateMarker(status, "work-step-glyph");
  }

  function safeItemKind(kind) {
    return ["user", "assistant", "status", "info", "warning", "error", "approval", "notice"].includes(kind)
      ? kind
      : "status";
  }

  function safeStatus(status) {
    return ["running", "complete", "completed", "failed", "error", "pending"].includes(status) ? status : "neutral";
  }

  /** The same words the mode switch uses; raw protocol modes never reach a label. */
  function friendlyModeLabel(mode) {
    const value = String(mode || "").toLowerCase();
    if (value === "readonly") {
      return "Read-only";
    }
    if (value === "auto") {
      return "Auto-approve";
    }
    if (value === "review") {
      return "Review changes";
    }
    return value ? value.replace(/_/g, " ") : "";
  }

  function friendlyItemLabel(kind, title) {
    if (kind === "error") {
      return "Could not continue";
    }
    if (kind === "approval") {
      return "Approval needed";
    }
    if (kind === "warning") {
      return "Warning";
    }
    return title || "Update";
  }

  function friendlyStatus(status) {
    if (status === "completed_unverified") return "Finished · unverified";
    const normalized = String(status || "").replaceAll("_", " ");
    if (["complete", "completed", "ok"].includes(normalized)) {
      return "Done";
    }
    if (["failed", "error"].includes(normalized)) {
      return "Failed";
    }
    return normalized || "Details";
  }

  function setText(id, value) {
    const node = document.getElementById(id);
    const next = String(value || "");
    if (node && node.textContent !== next) {
      node.textContent = next;
    }
  }

  function setTone(id, tone) {
    const node = document.getElementById(id);
    if (!node) {
      return;
    }
    node.classList.remove("ready", "attention", "neutral");
    node.classList.add(["ready", "attention", "neutral"].includes(tone) ? tone : "neutral");
  }

  function isSurface(value) {
    return value === "task"
      || value === "forge"
      || value === "browser"
      || value === "history"
      || value === "settings"
      || value === "activity"
      || value === "models";
  }

  applyStaticIcons();
  showSurface(surface);
  autoGrowComposer();
  syncModeControl();
  syncWorkflowContext();
  updateSubmitState();
  if (taskPending) {
    armPendingAckTimer();
  }
  host.postMessage({ type: "ready" });
})();
