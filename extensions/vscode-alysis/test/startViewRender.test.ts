import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

import { JSDOM, VirtualConsole } from "jsdom";

const ROOT = resolve(__dirname, "../..");

/** Every mounted window is closed after the run so webview timers cannot keep the process alive. */
const mountedWindows: any[] = [];
test.after(() => {
  for (const window of mountedWindows) {
    try {
      window.close();
    } catch {
      // ignore
    }
  }
});

function mountStartView(initialState?: any, portableHost = false): { window: any; messages: any[]; errors: any[]; getPersistedState(): any } {
  const provider = readFileSync(resolve(ROOT, "media/startView.html"), "utf8");
  const body = provider.match(/<body>([\s\S]*?)<script nonce/);
  if (!body) {
    throw new Error("could not extract Start Here body");
  }
  const errors: any[] = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (error) => errors.push(error));
  const dom = new JSDOM(`<!DOCTYPE html><html><body>${body[1]}</body></html>`, {
    runScripts: "outside-only",
    pretendToBeVisual: true,
    virtualConsole
  });
  const window: any = dom.window;
  const messages: any[] = [];
  let persistedState = initialState;
  window.acquireVsCodeApi = () => ({
    getState: () => persistedState,
    setState: (value: any) => {
      persistedState = value;
    },
    postMessage: (message: any) => messages.push(message)
  });
  if (portableHost) window.alysisHost = window.acquireVsCodeApi();
  window.eval(readFileSync(resolve(ROOT, "media/startView.js"), "utf8"));
  mountedWindows.push(window);
  return { window, messages, errors, getPersistedState: () => persistedState };
}

function postState(window: any, conversation: Record<string, unknown>, extra: Record<string, unknown> = {}): void {
  window.dispatchEvent(new window.MessageEvent("message", {
    data: {
      type: "state",
      state: {
        mode: "review",
        engine: { tone: "ready", detail: "Connected" },
        workspace: { tone: "ready", detail: "Trusted" },
        provider: { tone: "ready", detail: "Configured" },
        ready: true,
        readyReason: "Provider selection is ready.",
        conversation,
        ...extra
      }
    }
  }));
}

test("worktree bar renders real state, dispatches actions, and gates active work", () => {
  const { window, messages, errors } = mountStartView();
  const git = { available: true, branch: "feature/<script>", root: "/workspace/worktree", isWorktree: true,
    additions: 18, deletions: 4, untracked: 2, files: 5, canMove: true, busy: false };
  postState(window, { sessionId: "chat", items: [], running: false }, { gitWorkspace: git });
  const document = window.document;
  assert.equal(document.querySelector("#sessionWorkspaceBar").hidden, false);
  assert.equal(document.querySelector("#barGitAdded").textContent, "+18");
  assert.match(document.querySelector("#barWorkspaceDetail").textContent, /Worktree.*feature\/<script>.*2 new files/);
  assert.equal(document.querySelector("#barWorkspaceDetail script"), null);
  for (const id of ["barNewWorktree", "barMoveWorktree", "barGitChanges"]) document.getElementById(id).click();
  assert.deepEqual(messages.filter((message) => message.type === "worktree").map((message) => message.action), ["new", "move", "review"]);
  postState(window, { sessionId: "chat", items: [], running: true }, { gitWorkspace: { ...git, busy: true } });
  assert.equal(document.querySelector("#barMoveWorktree").disabled, true);
  assert.equal(document.querySelector("#barNewWorktree").disabled, true);
  assert.equal(document.querySelector("#barGitChanges").disabled, false);
  assert.deepEqual(errors, []);
});

test("settings search filters by group and copy, clears with Escape, and preserves the draft", () => {
  const { window, errors } = mountStartView();
  postState(window, { sessionId: null, items: [], running: false });
  const doc = window.document;
  const draft = doc.querySelector("#taskInput");
  draft.value = "Keep this UI draft";
  draft.dispatchEvent(new window.Event("input", { bubbles: true }));
  window.dispatchEvent(new window.MessageEvent("message", { data: { type: "surface.show", surface: "settings" } }));
  const search = doc.querySelector("#settingsSearch");
  search.value = "SAFETY";
  search.dispatchEvent(new window.Event("input"));
  assert.equal(doc.querySelectorAll(".settings-group:not(.settings-filtered)").length, 1);
  assert.match(doc.querySelector(".settings-group:not(.settings-filtered) h2").textContent, /Safety/);
  assert.equal(doc.querySelector("#settingsSearchStatus").textContent, "2 matching settings");
  search.value = "nothing-matches-this";
  search.dispatchEvent(new window.Event("input"));
  assert.equal(doc.querySelector("#settingsEmpty").hidden, false);
  doc.querySelector("#settingsClear").click();
  assert.equal(doc.activeElement, search);
  assert.equal(doc.querySelectorAll(".settings-filtered").length, 0);
  search.value = "provider";
  search.dispatchEvent(new window.Event("input"));
  search.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.equal(search.value, "");
  assert.equal(doc.querySelector("#settingsSurface").hidden, false);
  search.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.equal(doc.querySelector("#taskSurface").hidden, false);
  assert.equal(draft.value, "Keep this UI draft");
  assert.deepEqual(errors, []);
});

test("settings filtering never reveals host-hidden Forge controls or enables unavailable actions", () => {
  const { window } = mountStartView();
  const doc = window.document;
  postState(window, { sessionId: null, items: [], running: false }, { forgeEnabled: false, commands: [] });
  const search = doc.querySelector("#settingsSearch");
  search.value = "Forge";
  search.dispatchEvent(new window.Event("input"));
  assert.equal(doc.querySelector('#settingsSurface [data-surface="forge"]').hidden, true);
  assert.equal(doc.querySelector('[data-command="alysis.manageForgeAssets"]').disabled, true);
  search.value = "";
  search.dispatchEvent(new window.Event("input"));
  assert.equal(doc.querySelector('#settingsSurface [data-surface="forge"]').hidden, true);
});

test("provider disclosures and connection menus retain a complete keyboard path", () => {
  const { window, messages, errors } = mountStartView();
  const doc = window.document;
  postState(window, { sessionId: null, items: [], running: false }, {
    models: { supported: true, loaded: true, activeProfile: "openai", providers: [
      { key: "openai", label: "OpenAI", host: "api.openai.com", models: ["model-a"], profileName: "openai", connected: true },
      { key: "anthropic", label: "Anthropic", host: "api.anthropic.com", models: ["model-b"], profileName: "anthropic", connected: false }
    ], connections: [{ profile: "openai", model: "model-a", active: true, hasKey: true, storedInVsCode: true }] }
  });
  window.dispatchEvent(new window.MessageEvent("message", { data: { type: "surface.show", surface: "models" } }));
  doc.querySelector(".provider-head").click();
  assert.equal(doc.activeElement, doc.querySelector(".provider-head"));
  assert.equal(doc.activeElement.getAttribute("aria-expanded"), "true");
  doc.querySelector(".connection-more").dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
  assert.equal(doc.activeElement.textContent, "Replace key");
  doc.activeElement.dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
  assert.equal(doc.activeElement.textContent, "Remove key");
  doc.activeElement.dispatchEvent(new window.KeyboardEvent("keydown", { key: "End", bubbles: true }));
  assert.match(doc.activeElement.textContent, /Reconnect/);
  doc.activeElement.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.equal(doc.querySelector(".connection-menu"), null);
  assert.equal(doc.activeElement, doc.querySelector(".connection-more"));
  assert.equal(messages.some((message) => message.type === "provider.key"), false);
  assert.deepEqual(errors, []);
});

test("Start Here keeps one live conversation above the pinned composer", () => {
  const { window } = mountStartView();
  postState(window, {
    sessionId: "session-1",
    mode: "review",
    jobStatus: "running",
    running: true,
    items: [
      { id: "u1", kind: "user", title: "You", text: "Please review this", status: "", approvalId: null },
      { id: "t1", kind: "tool", title: "Reading files", text: "src/app.ts", status: "running", approvalId: null },
      { id: "a1", kind: "assistant", title: "Alysis Code", text: "I found the issue.", status: "complete", approvalId: null }
    ]
  });

  const document = window.document;
  assert.equal(document.querySelector(".sidebar-top-bar").hidden, true, "VS Code supplies the title and navigation");
  assert.equal(document.querySelector("#welcome").hidden, true);
  assert.equal(document.querySelector("#conversation").hidden, false);
  assert.equal(document.querySelectorAll("#conversationItems .conversation-item").length, 3);
  assert.equal(document.querySelector("#taskMode").disabled, true);
  assert.equal(document.querySelector("#submitTask")?.textContent, "Queue");
  assert.equal(
    document.querySelector("#submitTask")?.closest(".composer-footer") !== null,
    true,
    "send shares the model and persona row instead of reserving an extra line in the input"
  );
  assert.equal(document.querySelector("#taskForm").getAttribute("aria-busy"), "false");
  assert.equal(document.querySelector("#cancelTask").hidden, false);
  assert.equal(document.querySelector(".activity-item").open, false, "tool detail is collapsed by default");
});

test("agent work is grouped into calm, human-readable progress instead of raw tool rows", () => {
  const { window, errors } = mountStartView();
  postState(window, {
    sessionId: "session-progress",
    mode: "review",
    jobStatus: "running",
    running: true,
    items: [
      { id: "u1", kind: "user", title: "You", text: "Fix the broken form", status: "", approvalId: null },
      { id: "t1", kind: "tool", title: "rs.read", toolName: "rs.read", toolInput: "{\"path\":\"src/chat/form.ts\"}", text: "loaded", status: "ok", approvalId: null },
      { id: "t2", kind: "tool", title: "codebase_search", toolName: "codebase_search", toolInput: "{\"query\":\"submit handler\"}", text: "2 matches", status: "ok", approvalId: null },
      { id: "t3", kind: "tool", title: "apply_patch", toolName: "apply_patch", toolInput: "{\"path\":\"src/chat/form.ts\"}", text: "editing", status: "running", approvalId: null }
    ]
  });

  const document = window.document;
  const progress = document.querySelector(".work-progress");
  const summary = progress?.querySelector(":scope > summary");
  assert.equal(document.querySelectorAll(".work-progress").length, 1, "consecutive tool calls become one progress surface");
  assert.match(summary?.textContent || "", /Making changes/);
  assert.match(summary?.textContent || "", /3 steps/);
  assert.doesNotMatch(summary?.textContent || "", /rs\.read|codebase_search|apply_patch/);
  assert.equal(progress?.open, false, "the compact progress surface stays collapsed by default");
  assert.equal(document.querySelectorAll(".work-step").length, 3);
  assert.equal(document.querySelectorAll(".work-step-technical").length, 3);
  assert.match(document.querySelector(".work-step-context")?.textContent || "", /chat\/form\.ts/);
  assert.match(document.querySelector(".work-step-technical pre")?.textContent || "", /Tool: rs\.read/);
  assert.deepEqual(errors, []);
});

test("semantic activity uses the backend display title without exposing protocol or patch fields", () => {
  const { window, errors } = mountStartView();
  postState(window, {
    sessionId: "session-semantic",
    mode: "review",
    jobStatus: "running",
    running: true,
    items: [
      { id: "u1", kind: "user", title: "You", text: "Inspect the change", status: "", approvalId: null },
      {
        id: "a1",
        kind: "tool",
        title: "Inspect Git changes",
        toolName: "git",
        toolInput: "src/app.ts",
        semanticActivity: true,
        text: "2 files · +10 · −3",
        status: "running",
        approvalId: null
      }
    ]
  });

  const document = window.document;
  assert.equal(
    document.querySelector(".work-progress > summary .work-progress-title")?.textContent,
    "Inspect Git changes"
  );
  const visible = document.querySelector("#conversationItems")?.textContent || "";
  assert.match(visible, /Inspect Git changes/);
  assert.doesNotMatch(visible, /activity_update|display_title|rs\.read|diff --git|patch:/);
  assert.deepEqual(errors, []);
});

test("thinking reuses the last work disclosure and settles when the answer arrives", () => {
  const { window } = mountStartView();
  postState(window, {
    sessionId: "session-progress",
    mode: "review",
    jobStatus: "running",
    running: true,
    items: [
      { id: "u1", kind: "user", title: "You", text: "Inspect this", status: "", approvalId: null },
      { id: "t1", kind: "tool", title: "rs.read", toolName: "rs.read", toolInput: "README.md", text: "done", status: "ok", approvalId: null }
    ]
  });

  const document = window.document;
  const progress = document.querySelector(".work-progress");
  assert.equal(document.querySelectorAll(".work-progress").length, 1, "idle model time does not stack a second activity row");
  assert.equal(progress?.querySelector(".work-progress-title")?.textContent, "Thinking");
  assert.equal(progress?.dataset.phase, "thinking");
  assert.equal(progress?.querySelector(".work-step-status")?.textContent, "Done", "completed work remains available inside the disclosure");

  postState(window, {
    sessionId: "session-progress",
    mode: "review",
    jobStatus: "completed",
    running: false,
    items: [
      { id: "u1", kind: "user", title: "You", text: "Inspect this", status: "", approvalId: null },
      { id: "t1", kind: "tool", title: "rs.read", toolName: "rs.read", toolInput: "README.md", text: "done", status: "ok", approvalId: null },
      { id: "a1", kind: "assistant", title: "Alysis Code", text: "Everything looks good.", status: "complete", approvalId: null }
    ]
  });
  assert.equal(document.querySelectorAll(".work-progress-thinking").length, 0);
  assert.equal(document.querySelector(".work-progress"), progress, "the work history stays mounted when thinking ends");
  assert.equal(progress?.querySelector(".work-progress-title")?.textContent, "Read files");
  assert.equal(progress?.classList.contains("running"), false);
  assert.match(document.querySelector(".message.assistant")?.textContent || "", /Everything looks good/);
});

test("an expanded progress card stays open while live tool updates arrive", () => {
  const { window } = mountStartView();
  const conversation = {
    sessionId: "session-progress",
    mode: "review",
    jobStatus: "running",
    running: true,
    items: [
      { id: "u1", kind: "user", title: "You", text: "Trace this", status: "", approvalId: null },
      { id: "t1", kind: "tool", title: "rs.read", toolName: "rs.read", toolInput: "src/app.ts", text: "line 1", status: "running", approvalId: null }
    ]
  };
  postState(window, conversation);
  const first = window.document.querySelector(".work-progress");
  first.open = true;
  const marker = first.querySelector(".work-progress-marker");
  const summary = first.querySelector(":scope > summary");
  summary.focus();

  conversation.items[1].text = "line 2";
  postState(window, conversation);
  const updated = window.document.querySelector(".work-progress");
  assert.equal(updated, first, "live updates keep the disclosure mounted");
  assert.equal(updated.querySelector(".work-progress-marker"), marker, "the animation is not restarted by tool output");
  assert.equal(window.document.activeElement, summary, "keyboard focus stays on the work disclosure");
  assert.equal(updated.open, true, "the user's disclosure choice survives the update");
  assert.match(updated.querySelector(".work-step-technical pre").textContent, /line 2/);

  conversation.items[1].status = "ok";
  postState(window, conversation);
  assert.equal(updated.querySelector(".work-progress-title").textContent, "Thinking");
  assert.equal(updated.querySelector(".work-progress-marker"), marker, "the same indicator continues between work and thinking");
  assert.equal(updated.querySelector(".work-step-status").textContent, "Done");

  conversation.jobStatus = "cancellation_requested";
  postState(window, conversation);
  assert.equal(updated.querySelector(".work-progress-title").textContent, "Stopping");
  assert.equal(window.document.querySelectorAll(".work-progress.running").length, 1);
  conversation.running = false;
  conversation.jobStatus = "cancelled";
  postState(window, conversation);
  assert.equal(window.document.querySelectorAll(".work-progress.running").length, 0);
});

test("thinking persists through empty answer events and yields to visible text or approval", () => {
  const { window, errors } = mountStartView();
  const document = window.document;
  const conversation: any = {
    sessionId: "session-thinking", mode: "review", jobStatus: "running", running: true,
    items: [{ id: "u1", kind: "user", text: "Inspect this" }]
  };
  postState(window, conversation);
  const thinking = document.querySelector(".work-progress-thinking");
  assert.equal(thinking?.getAttribute("aria-label"), "Alysis Code is thinking");
  conversation.items.push({ id: "turn1", kind: "tool", turnLevel: true, status: "running", title: "Working on request" });
  conversation.items.push({ id: "a1", kind: "assistant", status: "streaming", text: " " });
  postState(window, conversation);
  assert.equal(document.querySelector(".work-progress-thinking"), thinking, "transport events don't rebuild the indicator");
  assert.equal(document.querySelector(".message.assistant"), null, "no empty answer placeholder flashes");

  conversation.items[2].text = "I found the search component.";
  postState(window, conversation);
  assert.equal(document.querySelector(".work-progress-thinking"), null);
  assert.match(document.querySelector(".message.assistant").textContent, /I found the search component/);

  conversation.items[2].status = "complete";
  conversation.items.push({ id: "p1", kind: "approval", status: "pending", approvalId: "approval-1", text: "Save the change?" });
  postState(window, conversation);
  assert.equal(document.querySelector(".work-progress-thinking"), null, "waiting for the user isn't presented as thinking");

  conversation.items[3].status = "allow_once";
  postState(window, conversation);
  assert.equal(document.querySelectorAll(".work-progress-thinking").length, 1);
  conversation.running = false;
  conversation.jobStatus = "failed";
  postState(window, conversation);
  assert.equal(document.querySelector(".work-progress-thinking"), null, "thinking ends when the job fails");
  assert.deepEqual(errors, []);
});

test("the transcript is a log that announces only finalized items, never every token", () => {
  const { window, errors } = mountStartView();
  const document = window.document;
  const items = document.querySelector("#conversationItems");

  assert.equal(items.getAttribute("role"), "log");
  assert.equal(items.getAttribute("aria-live"), null, "a live transcript re-announces itself on every publish");
  assert.equal(items.getAttribute("aria-relevant"), null);
  assert.equal(document.querySelector("#liveAnnouncer").getAttribute("aria-live"), "polite");
  assert.equal(document.querySelector("#liveAnnouncer").getAttribute("aria-atomic"), "true");

  const conversation: any = {
    sessionId: "session-a11y",
    mode: "review",
    jobStatus: "running",
    running: true,
    items: [
      { id: "u1", kind: "user", title: "You", text: "Explain this", status: "", approvalId: null },
      { id: "a1", kind: "assistant", title: "Alysis Code", text: "Look", status: "streaming", approvalId: null }
    ]
  };
  postState(window, conversation);
  assert.equal(items.getAttribute("aria-busy"), "true");
  assert.equal(document.querySelector("#liveAnnouncer").textContent, "", "a streaming answer is not announced yet");

  conversation.items[1].text = "Looking at the file now";
  postState(window, conversation);
  assert.equal(document.querySelector("#liveAnnouncer").textContent, "", "each token must not re-announce");

  conversation.items[1].status = "complete";
  conversation.jobStatus = "completed";
  conversation.running = false;
  postState(window, conversation);
  assert.match(document.querySelector("#liveAnnouncer").textContent, /Looking at the file now/);
  assert.equal(items.getAttribute("aria-busy"), "false");
  assert.deepEqual(errors, []);
});

test("reconciling in place keeps node identity, keyboard focus, and the user's scroll position", () => {
  const { window, errors } = mountStartView();
  const document = window.document;
  const conversation: any = {
    sessionId: "session-focus",
    mode: "review",
    jobStatus: "running",
    running: true,
    items: [
      { id: "u1", kind: "user", title: "You", text: "Change the config", status: "", approvalId: null },
      {
        id: "ap1",
        kind: "approval",
        title: "Approval",
        text: "Write config.json?",
        status: "pending",
        approvalId: "approval-1",
        allowForSession: true
      },
      { id: "a1", kind: "assistant", title: "Alysis Code", text: "Working", status: "streaming", approvalId: null }
    ]
  };
  postState(window, conversation);

  const userNode = document.querySelector('[data-item-id="u1"]');
  const allowOnce = document.querySelector('[data-approval-decision="allow_once"]');
  allowOnce.focus();
  assert.equal(document.activeElement, allowOnce);

  const items = document.querySelector("#conversationItems");
  Object.defineProperty(items, "scrollHeight", { value: 2000, configurable: true });
  Object.defineProperty(items, "clientHeight", { value: 400, configurable: true });
  items.scrollTop = 100;
  items.dispatchEvent(new window.Event("scroll"));

  conversation.items[2].text = "Working on it";
  postState(window, conversation);

  assert.equal(document.querySelector('[data-item-id="u1"]'), userNode, "an unchanged item keeps its exact node");
  assert.equal(
    document.activeElement,
    document.querySelector('[data-approval-decision="allow_once"]'),
    "focus stays on the approval control while the answer streams"
  );
  assert.equal(items.scrollTop, 100, "a run must not drag the reader back to the bottom");
  assert.equal(document.querySelector("#jumpToLatest").hidden, false, "new content offers a jump instead");

  document.querySelector("#jumpToLatest").click();
  assert.equal(items.scrollTop, 2000);
  assert.equal(document.querySelector("#jumpToLatest").hidden, true);
  assert.deepEqual(errors, []);
});

test("readiness blockers pin above the composer and route their recovery commands", () => {
  const { window, messages, errors } = mountStartView();
  const document = window.document;

  window.dispatchEvent(new window.MessageEvent("message", {
    data: {
      type: "state",
      state: {
        mode: "review",
        engine: { tone: "attention", detail: "Locate the CLI" },
        workspace: { tone: "ready", detail: "Trusted" },
        provider: { tone: "attention", detail: "Choose a model" },
        ready: false,
        readyReason: "Alysis Code cannot reach its engine.",
        readiness: {
          ok: false,
          blockers: [
            {
              id: "engine-missing",
              severity: "error",
              title: "Alysis Code cannot reach its engine",
              detail: "The Alysis Code CLI could not be found on this computer.",
              actions: [
                { label: "Locate the CLI", command: "alysis.locateCli", primary: true },
                { label: "Setup guide", command: "alysis.openSetupGuide" }
              ]
            },
            {
              id: "workspace-untrusted",
              severity: "warning",
              title: "This folder is not trusted yet",
              detail: "Alysis Code cannot change files here until you trust this folder.",
              actions: [{ label: "Manage trust", command: "workbench.trust.manage", primary: true }]
            }
          ]
        },
        conversation: { sessionId: null, mode: null, jobStatus: "idle", running: false, items: [] }
      }
    }
  }));

  // On the empty start surface the hero explains the primary (error) blocker with its own actions;
  // the strip above the composer carries only the remaining blockers, so nothing is said twice.
  const hero = document.querySelector("#connectFirst");
  assert.equal(hero.hidden, false);
  assert.match(hero.textContent, /Alysis Code cannot reach its engine/);
  assert.deepEqual(
    Array.from(hero.querySelectorAll("#connectFirstActions button")).map((button: any) => button.textContent),
    ["Locate the CLI", "Setup guide"]
  );
  const strip = document.querySelector("#readinessStrip");
  assert.equal(strip.hidden, false);
  assert.equal(strip.querySelectorAll(".readiness-row").length, 1);
  assert.equal(strip.querySelector(".readiness-row").classList.contains("warning"), true);
  assert.doesNotMatch(strip.textContent, /Alysis Code cannot reach its engine/);
  assert.match(strip.textContent, /This folder is not trusted yet/);
  assert.equal(document.querySelector("#submitTask").disabled, true, "a blocked setup cannot accept a send");

  messages.length = 0;
  hero.querySelector("#connectFirstActions button").click();
  assert.equal(JSON.stringify(messages.at(-1)), JSON.stringify({ type: "command", command: "alysis.locateCli" }));
  strip.querySelector(".recovery-button.primary").click();
  assert.equal(JSON.stringify(messages.at(-1)), JSON.stringify({ type: "command", command: "workbench.trust.manage" }));

  // Once a conversation is open the hero is gone, so the strip shows every blocker.
  postState(window, {
    sessionId: "s1", mode: "review", jobStatus: "idle", running: false,
    items: [{ id: "u1", kind: "user", title: "You", text: "hi", status: "", approvalId: null }]
  }, {
    ready: false,
    readiness: {
      ok: false,
      blockers: [
        { id: "engine-missing", severity: "error", title: "Alysis Code cannot reach its engine", detail: "", actions: [] },
        { id: "workspace-untrusted", severity: "warning", title: "This folder is not trusted yet", detail: "", actions: [] }
      ]
    }
  });
  assert.equal(strip.querySelectorAll(".readiness-row").length, 2);

  window.dispatchEvent(new window.MessageEvent("message", {
    data: {
      type: "state",
      state: {
        mode: "review",
        engine: { tone: "ready", detail: "Connected" },
        workspace: { tone: "ready", detail: "Trusted" },
        provider: { tone: "ready", detail: "Ready" },
        ready: true,
        readyReason: "Ready",
        readiness: { ok: true, blockers: [] },
        conversation: { sessionId: null, mode: null, jobStatus: "idle", running: false, items: [] }
      }
    }
  }));
  assert.equal(document.querySelector("#readinessStrip").hidden, true, "a healthy setup shows no strip at all");
  assert.deepEqual(errors, []);
});

test("typed failures render as actionable cards, with a countdown for rate limits", () => {
  const { window, messages, errors } = mountStartView();
  const document = window.document;
  postState(window, {
    sessionId: "session-errors",
    mode: "review",
    jobStatus: "failed",
    running: false,
    items: [
      {
        id: "e1",
        kind: "error",
        title: "error",
        text: "raw provider payload",
        status: "",
        approvalId: null,
        errorKind: "auth_invalid",
        errorTitle: "Your provider key was rejected",
        errorDetail: "Anthropic refused the stored key for this profile.",
        errorActions: [{ label: "Update key", command: "alysis.configureProvider", primary: true }]
      },
      {
        id: "e2",
        kind: "error",
        title: "error",
        text: "429",
        status: "",
        approvalId: null,
        errorKind: "rate_limited",
        errorTitle: "The provider is rate limiting this key",
        errorDetail: "Requests are being throttled upstream.",
        errorActions: [],
        retryAfterSeconds: 45
      },
      {
        id: "e3",
        kind: "error",
        title: "error",
        text: "cancelled",
        status: "",
        approvalId: null,
        errorKind: "cancelled",
        errorTitle: "You stopped this task",
        errorDetail: "Nothing further was changed.",
        errorActions: []
      }
    ]
  });

  const auth = document.querySelector('[data-item-id="e1"]');
  assert.match(auth.querySelector(".message-author").textContent, /Your provider key was rejected/);
  assert.doesNotMatch(auth.textContent, /Could not continue/, "the generic heading is replaced by the typed title");
  assert.match(auth.textContent, /Anthropic refused the stored key/);

  messages.length = 0;
  auth.querySelector(".recovery-button").click();
  assert.equal(JSON.stringify(messages.at(-1)), JSON.stringify({ type: "command", command: "alysis.configureProvider" }));

  const limited = document.querySelector('[data-item-id="e2"]');
  assert.match(limited.querySelector(".error-countdown").textContent, /Try again in .*4[0-5]s/);

  const cancelled = document.querySelector('[data-item-id="e3"]');
  assert.equal(cancelled.classList.contains("error"), false, "a cancellation is a neutral notice, not an error");
  assert.equal(cancelled.classList.contains("notice"), true);
  assert.equal(cancelled.getAttribute("role"), null);
  assert.deepEqual(errors, []);
  window.close();
});

test("assistant code blocks carry copy, insert, apply, and local syntax highlighting", () => {
  const { window, messages, errors } = mountStartView();
  const document = window.document;
  postState(window, {
    sessionId: "session-code",
    mode: "review",
    jobStatus: "completed",
    running: false,
    items: [
      {
        id: "a1",
        kind: "assistant",
        title: "Alysis Code",
        text: "Try this:\n\n```ts\n// count things\nconst total = 42;\n```\n",
        status: "complete",
        approvalId: null
      }
    ]
  });

  const block = document.querySelector(".code-block");
  assert.equal(block.querySelector("code").textContent, "// count things\nconst total = 42;");
  assert.equal(block.querySelector(".code-block-language").textContent, "TypeScript");
  assert.equal(block.querySelectorAll(".tok-comment").length, 1);
  assert.equal(block.querySelectorAll(".tok-keyword").length >= 1, true);
  assert.equal(block.querySelectorAll(".tok-number").length, 1);
  assert.equal(block.querySelector("svg") !== null, true, "actions use inline SVG icons, not raw glyphs");

  const actions = Array.from(block.querySelectorAll(".code-action")) as any[];
  assert.deepEqual(actions.map((button) => button.textContent.replace(/\s+/g, "")), ["Copy", "Insert", "Apply"]);

  messages.length = 0;
  actions[0].click();
  assert.equal(
    JSON.stringify(messages.at(-1)),
    JSON.stringify({ type: "clipboard.copy", text: "// count things\nconst total = 42;" })
  );
  actions[1].click();
  assert.equal(
    JSON.stringify(messages.at(-1)),
    JSON.stringify({ type: "editor.insert", text: "// count things\nconst total = 42;" })
  );
  actions[2].click();
  assert.equal(
    JSON.stringify(messages.at(-1)),
    JSON.stringify({ type: "editor.applyCode", text: "// count things\nconst total = 42;", language: "js" })
  );
  assert.deepEqual(errors, []);
});

test("rich Markdown renders links, tables, lists, quotes, and clickable file mentions", () => {
  const { window, messages, errors } = mountStartView();
  const document = window.document;
  postState(window, {
    sessionId: "session-md",
    mode: "review",
    jobStatus: "completed",
    running: false,
    items: [
      {
        id: "a1",
        kind: "assistant",
        title: "Alysis Code",
        text: [
          "See [the docs](https://example.com/guide) and never [this](command:workbench.action.quit).",
          "",
          "| File | Change |",
          "| --- | --- |",
          "| a.ts | edited |",
          "",
          "1. first",
          "2. second",
          "   - nested",
          "",
          "> a quoted note",
          "",
          "~~old~~ and `src/chat/ChatController.ts`",
          "",
          "---"
        ].join("\n"),
        status: "complete",
        approvalId: null
      }
    ]
  });

  const body = document.querySelector('[data-item-id="a1"] .message-body');
  const link = body.querySelector("a.md-link");
  assert.equal(link.textContent, "the docs");
  assert.equal(link.getAttribute("href"), "https://example.com/guide");
  assert.equal(body.querySelectorAll("a").length, 1, "command: URLs never become links");
  assert.match(body.textContent, /command:workbench\.action\.quit/, "the unsafe target stays visible as text");

  messages.length = 0;
  link.click();
  assert.equal(
    JSON.stringify(messages.at(-1)),
    JSON.stringify({ type: "open.external", url: "https://example.com/guide" })
  );

  assert.equal(body.querySelectorAll("table.md-table tbody tr").length, 1);
  assert.equal(body.querySelector("table.md-table th").textContent, "File");
  assert.equal(body.querySelectorAll("ol > li").length, 2);
  assert.equal(body.querySelectorAll("ol ul > li").length, 1, "nested lists stay nested");
  assert.equal(body.querySelector("blockquote").textContent.trim(), "a quoted note");
  assert.equal(body.querySelector("del").textContent, "old");
  assert.equal(body.querySelectorAll("hr").length, 1);

  const mentions = Array.from(body.querySelectorAll(".file-mention")) as any[];
  const mention = mentions.find((node) => node.textContent === "src/chat/ChatController.ts");
  assert.ok(mention, "a path inside inline code becomes a one-click opener");
  assert.equal(mentions[0].textContent, "a.ts", "bare filenames in prose are openable too");
  messages.length = 0;
  mention.click();
  assert.equal(
    JSON.stringify(messages.at(-1)),
    JSON.stringify({ type: "open.file", path: "src/chat/ChatController.ts" })
  );
  assert.deepEqual(errors, []);
});

test("the composer recalls previous submissions with the arrow keys", () => {
  const { window, messages } = mountStartView();
  const document = window.document;
  postState(window, { sessionId: null, mode: "review", jobStatus: "idle", running: false, items: [] });
  const input = document.querySelector("#taskInput");

  const submit = (text: string): void => {
    input.value = text;
    input.dispatchEvent(new window.Event("input"));
    document.querySelector("#taskForm").dispatchEvent(new window.Event("submit"));
    const sent = messages.filter((message: any) => message.type === "task.submit").at(-1);
    window.dispatchEvent(new window.MessageEvent("message", {
      data: { type: "task.result", started: true, requestId: sent?.requestId }
    }));
  };
  submit("first task");
  submit("second task");

  assert.equal(input.value, "");
  input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }));
  assert.equal(input.value, "second task");
  input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }));
  assert.equal(input.value, "first task");
  input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
  assert.equal(input.value, "second task");
  input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
  assert.equal(input.value, "", "coming back past the newest entry restores the draft");
});

test("dropping files attaches images and turns anything else into an @ mention", () => {
  const { window, messages, errors } = mountStartView();
  const document = window.document;
  postState(window, { sessionId: null, mode: "review", jobStatus: "idle", running: false, items: [] });

  const drop = (uriList: string): void => {
    const event = new window.Event("drop", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "dataTransfer", {
      value: { getData: (type: string) => (type === "text/uri-list" ? uriList : ""), files: [] }
    });
    document.querySelector("#taskForm").dispatchEvent(event);
  };

  messages.length = 0;
  drop("file:///repo/screenshots/bug.png");
  assert.equal(
    JSON.stringify(messages.at(-1)),
    JSON.stringify({ type: "image.attach", uris: ["file:///repo/screenshots/bug.png"] })
  );
  const basket = document.querySelector("#imageBasket");
  assert.equal(basket.hidden, true, "the agent must accept an image before it appears attached");

  messages.length = 0;
  drop("file:///repo/src/notes.md");
  assert.equal(messages.length, 0, "a non-image drop never posts an image message");
  assert.match(document.querySelector("#taskInput").value, /@\/repo\/src\/notes\.md/);
  assert.deepEqual(errors, []);
});

test("Start Here remains responsive and error-free across 1,000 rapid state updates", () => {
  const { window, errors } = mountStartView();
  const started = performance.now();
  for (let index = 0; index < 1_000; index += 1) {
    postState(window, {
      sessionId: "stress-session",
      mode: "review",
      jobStatus: index === 999 ? "completed" : "running",
      running: index !== 999,
      items: [
        { id: `a-${index}`, kind: "assistant", title: "Alysis Code", text: `Update ${index}`, status: "complete", approvalId: null }
      ]
    });
  }
  const elapsed = performance.now() - started;

  assert.match(window.document.querySelector("#conversationItems").textContent, /Update 999/);
  assert.equal(window.document.querySelector("#taskInput").disabled, false);
  assert.deepEqual(errors, [], "state rendering must not throw into the webview event loop");
  assert.ok(elapsed < 10_000, `1,000 renders took ${elapsed.toFixed(1)}ms`);
});

test("streaming preserves focused setup actions until readiness changes", () => {
  const { window, messages, errors } = mountStartView();
  const blocker = {
    id: "provider", title: "Connect a model", detail: "Choose a provider",
    actions: [{ label: "Configure provider", command: "alysis.configureProvider" }]
  };
  const extra = { ready: false, readiness: { ok: false, blockers: [blocker] } };
  postState(window, { sessionId: "chat", items: [], running: false }, extra);
  const button = window.document.querySelector("#connectFirstActions button");
  button.focus();
  postState(window, {
    sessionId: "chat", running: true,
    items: [{ id: "reply", kind: "assistant", title: "Alysis Code", text: "Progress", status: "running" }]
  }, extra);
  assert.equal(window.document.querySelector("#connectFirstActions button"), button);
  assert.equal(window.document.activeElement, button);
  button.click();
  assert.equal(messages.at(-1)?.command, "alysis.configureProvider");
  postState(window, { sessionId: "chat", items: [], running: false }, {
    ready: false, readiness: { ok: false, blockers: [{ ...blocker, title: "Restart engine",
      actions: [{ label: "Reconnect", command: "alysis.restartEngine" }] }] }
  });
  const replacement = window.document.querySelector("#connectFirstActions button");
  assert.notEqual(replacement, button);
  assert.equal(replacement.textContent, "Reconnect");
  replacement.click();
  assert.equal(messages.at(-1)?.command, "alysis.restartEngine");
  assert.deepEqual(errors, []);
});

test("the composer clears on Send and Enter, stays clear on success, and cannot double-start", () => {
  const buttonMount = mountStartView();
  postState(buttonMount.window, {
    sessionId: null,
    mode: "readonly",
    jobStatus: "idle",
    running: false,
    items: []
  });
  const buttonInput = buttonMount.window.document.querySelector("#taskInput");
  const send = buttonMount.window.document.querySelector("#submitTask");
  buttonInput.value = "Check the connection";
  buttonInput.dispatchEvent(new buttonMount.window.Event("input", { bubbles: true }));
  assert.equal(send.disabled, false);
  send.click();
  buttonInput.dispatchEvent(new buttonMount.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  assert.equal(buttonInput.value, "", "Send clears the handed-off message immediately");
  const buttonSubmits = buttonMount.messages.filter((message) => message.type === "task.submit");
  assert.equal(buttonSubmits.length, 1);
  assert.equal(buttonSubmits[0].instruction, "Check the connection");
  assert.equal(buttonSubmits[0].mode, "review");
  assert.equal(buttonSubmits[0].workflow, "chat");
  buttonMount.window.dispatchEvent(new buttonMount.window.MessageEvent("message", {
    data: { type: "task.result", started: true }
  }));
  assert.equal(buttonInput.value, "", "an accepted send remains cleared after acknowledgement");

  const enterMount = mountStartView();
  postState(enterMount.window, {
    sessionId: null,
    mode: "readonly",
    jobStatus: "idle",
    running: false,
    items: []
  });
  const enterInput = enterMount.window.document.querySelector("#taskInput");
  enterInput.value = "Send with Enter";
  enterInput.dispatchEvent(new enterMount.window.Event("input", { bubbles: true }));
  enterInput.dispatchEvent(new enterMount.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  assert.equal(enterInput.value, "", "Enter clears the handed-off message immediately");
  const enterSubmits = enterMount.messages.filter((message) => message.type === "task.submit");
  assert.equal(enterSubmits.length, 1);
  assert.equal(enterSubmits[0].instruction, "Send with Enter");
  assert.equal(enterSubmits[0].mode, "review");
  assert.equal(enterSubmits[0].workflow, "chat");
  enterMount.window.dispatchEvent(new enterMount.window.MessageEvent("message", {
    data: { type: "task.result", started: true, requestId: enterSubmits[0].requestId }
  }));
});

test("a failed task renders an error, preserves the draft, and releases composer busy state", () => {
  const { window } = mountStartView();
  postState(window, {
    sessionId: null,
    mode: "readonly",
    jobStatus: "idle",
    running: false,
    items: []
  });
  const document = window.document;
  const input = document.querySelector("#taskInput");
  const send = document.querySelector("#submitTask");
  input.value = "Explain this project";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  send.click();
  assert.equal(document.querySelector("#taskForm").getAttribute("aria-busy"), "true");
  assert.equal(input.value, "", "the failed instruction is held off-screen while its result is pending");

  postState(window, {
    sessionId: null,
    mode: null,
    jobStatus: "idle",
    running: false,
    items: [
      { id: "u1", kind: "user", title: "You", text: "Explain this project", status: "", approvalId: null },
      { id: "e1", kind: "error", title: "Could not continue", text: "Subscription is not configured (config_error).", status: "", approvalId: null }
    ]
  });
  window.dispatchEvent(new window.MessageEvent("message", {
    data: { type: "task.result", started: false }
  }));

  assert.equal(document.querySelector("#taskForm").getAttribute("aria-busy"), "false");
  assert.equal(input.value, "Explain this project");
  assert.equal(send.disabled, false);
  assert.match(document.querySelector("#conversationItems .message.error")?.textContent || "", /config_error/);
});

test("a rejected send restores its instruction without losing text typed while pending", () => {
  const { window } = mountStartView();
  postState(window, {
    sessionId: null,
    mode: "review",
    jobStatus: "idle",
    running: false,
    items: []
  });
  const input = window.document.querySelector("#taskInput");
  input.value = "First instruction";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  window.document.querySelector("#submitTask").click();

  input.value = "New context typed while waiting";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  window.dispatchEvent(new window.MessageEvent("message", {
    data: { type: "task.result", started: false }
  }));

  assert.equal(input.value, "First instruction\n\nNew context typed while waiting");
});

test("a running chat accepts one durable queued follow-up and correlates its acknowledgement", () => {
  const { window, messages } = mountStartView();
  postState(window, {
    sessionId: "chat-session",
    mode: "review",
    jobStatus: "running",
    running: true,
    items: [{ id: "u1", kind: "user", title: "You", text: "First task", status: "", approvalId: null }]
  });
  const input = window.document.querySelector("#taskInput");
  const send = window.document.querySelector("#submitTask");
  input.value = "Check the tests next";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  assert.equal(send.disabled, false);
  assert.equal(send.textContent, "Queue");
  send.click();
  const submitted = messages.find((message: any) => message.type === "task.submit");
  assert.equal(submitted.instruction, "Check the tests next");
  assert.equal(typeof submitted.requestId, "string");
  assert.equal(send.disabled, true, "only one follow-up can be awaiting host acknowledgement");

  window.dispatchEvent(new window.MessageEvent("message", {
    data: { type: "task.result", started: false, requestId: "stale-request" }
  }));
  assert.equal(input.value, "", "a stale acknowledgement cannot alter the pending hand-off");
  window.dispatchEvent(new window.MessageEvent("message", {
    data: { type: "task.result", started: false, requestId: submitted.requestId }
  }));
  assert.equal(input.value, "Check the tests next");
  assert.equal(send.disabled, false);
});

test("a webview reload keeps an unacknowledged submission off-screen and restores it only on rejection", () => {
  const first = mountStartView();
  postState(first.window, {
    sessionId: null,
    mode: "review",
    jobStatus: "idle",
    running: false,
    items: []
  });
  const firstInput = first.window.document.querySelector("#taskInput");
  firstInput.value = "Do not lose this message";
  firstInput.dispatchEvent(new first.window.Event("input", { bubbles: true }));
  first.window.document.querySelector("#submitTask").click();
  const persisted = first.getPersistedState();
  assert.equal(persisted.pendingInstruction, "Do not lose this message");
  first.window.close();

  const reloaded = mountStartView(persisted);
  postState(reloaded.window, {
    sessionId: null,
    mode: "review",
    jobStatus: "idle",
    running: false,
    items: []
  });
  const restoredInput = reloaded.window.document.querySelector("#taskInput");
  assert.equal(restoredInput.value, "", "a reload must not make a possibly accepted task look unsent");
  assert.equal(reloaded.window.document.querySelector("#submitTask").disabled, true);
  assert.equal(
    reloaded.messages.filter((message) => message.type === "task.submit").length,
    0,
    "reload never resubmits automatically"
  );

  reloaded.window.dispatchEvent(new reloaded.window.MessageEvent("message", {
    data: { type: "task.result", started: false, requestId: persisted.pendingRequestId }
  }));
  assert.equal(restoredInput.value, "Do not lose this message");
  assert.equal(reloaded.window.document.querySelector("#submitTask").disabled, false);
  assert.equal(reloaded.getPersistedState().pendingInstruction, "");
});

test("Start Here prefill preserves the draft, focuses the sidebar, and never submits automatically", () => {
  const { window, messages } = mountStartView();
  const input = window.document.querySelector("#taskInput");
  input.value = "Existing context";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  const before = messages.filter((message) => message.type === "task.submit").length;

  window.dispatchEvent(new window.MessageEvent("message", {
    data: { type: "composer.prefill", text: "Selected code" }
  }));

  assert.equal(input.value, "Existing context\n\nSelected code");
  assert.equal(window.document.activeElement, input);
  assert.equal(messages.filter((message) => message.type === "task.submit").length, before);
});

test("permissions use the agent's values without a retired Plan/Act toggle", () => {
  const { window, messages } = mountStartView();
  postState(window, {
    sessionId: null,
    mode: null,
    jobStatus: "idle",
    running: false,
    items: []
  });

  const document = window.document;
  assert.equal(document.querySelector("#actMode"), null);
  assert.equal(document.querySelector("#planMode"), null);
  document.querySelector("#permissionStrip").click();
  document.querySelector('[data-permission="readonly"]').click();
  assert.equal(document.querySelector("#taskMode").value, "review", "the previous permission stays visible until host confirmation");
  assert.equal(messages.at(-1).message.mode, "readonly");
  postState(window, { sessionId: null, mode: null, items: [] }, { mode: "readonly" });
  assert.equal(document.querySelector("#taskMode").value, "readonly");
  assert.equal(document.querySelector("#permissionLabel").textContent, "Read-only");
  assert.equal(messages.at(-1).message.mode, "readonly");

  document.querySelector("#permissionStrip").click();
  document.querySelector('[data-permission="review"]').click();
  assert.equal(document.querySelector("#taskMode").value, "readonly");
  postState(window, { sessionId: null, mode: null, items: [] }, { mode: "review" });
  assert.equal(document.querySelector("#taskMode").value, "review");
  document.querySelector("#permissionStrip").click();
  assert.equal(document.querySelector("#taskMode").value, "review", "opening the menu never broadens permissions");
  assert.equal(document.querySelector("#permissionPalette").hidden, false);
  document.querySelector('[data-permission="auto"]').click();
  assert.equal(document.querySelector("#taskMode").value, "review");
  // A rejected change republishes the previous mode and cannot broaden it.
  postState(window, { sessionId: null, mode: null, items: [] }, { mode: "review" });
  assert.equal(document.querySelector("#permissionLabel").textContent, "Review changes");
  postState(window, { sessionId: null, mode: null, items: [] }, { mode: "auto" });
  assert.equal(document.querySelector("#taskMode").value, "auto");
  assert.equal(document.querySelector("#permissionLabel").textContent, "Auto-approve");
  assert.equal(messages.at(-1).message.mode, "auto");
});

test("image badges follow confirmed agent baskets and cannot leak between tasks", () => {
  const { window, messages } = mountStartView();
  const conversation = { sessionId: "images-1", mode: "review", jobStatus: "idle", running: false, items: [] };
  postState(window, conversation);
  const basket = window.document.querySelector("#imageBasket");
  const confirm = (sessionId: string, names: string[]) => window.dispatchEvent(new window.MessageEvent("message", {
    data: { type: "image.basket", sessionId, names }
  }));
  confirm("images-1", ["accepted.png"]);
  assert.equal(basket.hidden, false);
  assert.match(basket.textContent, /accepted.png/);
  basket.querySelector("button").click();
  assert.equal(messages.at(-1).command, "alysis.backend.session.images.clear");
  assert.equal(basket.hidden, false, "cancelled or failed clears must retain confirmed attachments");
  confirm("images-1", []);
  assert.equal(basket.hidden, true, "consumed or cleared images leave the next-message basket");
  confirm("images-1", ["next.png"]);
  postState(window, { ...conversation, sessionId: "images-2" });
  assert.equal(basket.hidden, true);
  confirm("images-1", ["late.png"]);
  assert.equal(basket.hidden, true, "late responses from a previous task are ignored");
});

test("Forge and settings controls respect the host's enabled workflows and availability", () => {
  const { window } = mountStartView();
  const conversation = { sessionId: "session-1", mode: "review", jobStatus: "idle", running: false, items: [] };
  postState(window, conversation, { forgeEnabled: false, commands: [
    { command: "alysis.manageToolsSkills", available: false, unavailableReason: "Agent does not support tools management." }
  ] });
  assert.equal(window.document.querySelector('[data-surface="forge"]').hidden, true);
  const unsupported = window.document.querySelector('[data-command="alysis.manageToolsSkills"]');
  assert.equal(unsupported.disabled, true);
  assert.match(unsupported.title, /does not support/);
  postState(window, conversation, { forgeEnabled: true, commands: [
    { command: "alysis.manageToolsSkills", available: true }
  ] });
  assert.equal(unsupported.disabled, false);
  window.dispatchEvent(new window.MessageEvent("message", { data: { type: "surface.show", surface: "forge" } }));
  assert.equal(window.document.querySelector("#permissionStrip").hidden, true, "chat permissions do not control Forge");
});

test("host permissions override a stale saved composer selection and stay locked during work", () => {
  const { window, messages } = mountStartView({ mode: "auto", agentMode: "act", autoApprove: true });
  postState(window, { sessionId: "s1", mode: "readonly", running: true, jobStatus: "running", items: [] });
  const doc = window.document;
  assert.equal(doc.querySelector("#permissionLabel").textContent, "Read-only");
  assert.equal(doc.querySelector("#permissionStrip").disabled, true);
  doc.querySelector('[data-permission="auto"]').click();
  assert.equal(messages.some((message: any) => message.message?.type === "mode.set"), false);
  postState(window, { sessionId: null, mode: null, running: false, items: [] }, { mode: "review" });
  assert.equal(doc.querySelector("#taskMode").value, "review", "the configured default wins after leaving a session");
});

test("history search filters by title and detail, keeps row identity on stream updates, and recovers from no matches", () => {
  const { window, messages, errors } = mountStartView();
  const doc = window.document;
  const chat = { sessionId: "one", items: [], running: false };
  const recentTasks = [
    { sessionId: "one", title: "Fix search", detail: "Completed today", current: true, canResume: false },
    { sessionId: "two", title: "Review auth", detail: "Paused yesterday", current: false, canResume: true }
  ];
  postState(window, chat, { recentTasks });
  const first = doc.querySelector("#historyList .task-card");
  postState(window, { ...chat, items: [{ id: "u", kind: "user", text: "a new token" }] }, { recentTasks });
  assert.equal(doc.querySelector("#historyList .task-card"), first);
  const input = doc.querySelector("#historySearch");
  input.value = "YESTERDAY";
  input.dispatchEvent(new window.Event("input"));
  assert.equal(doc.querySelectorAll("#historyList .task-card").length, 1);
  doc.querySelector("#historyList .task-card").click();
  assert.equal(messages.at(-1).sessionId, "two");
  input.value = "does not exist";
  input.dispatchEvent(new window.Event("input"));
  assert.match(doc.querySelector("#historyList").textContent, /No matching tasks/);
  doc.querySelector("#historyList .secondary-action").click();
  assert.equal(doc.querySelectorAll("#historyList .task-card").length, 2);
  doc.querySelector("#historyCurrent").click();
  assert.equal(doc.querySelector("#historyList .task-card").getAttribute("aria-current"), "true");
  assert.equal(doc.querySelectorAll("#historyList .task-card").length, 1);
  assert.deepEqual(errors, []);
});

test("approval summary focuses evidence, reports line counts, and disappears when the request resolves", () => {
  const { window, errors } = mountStartView();
  const doc = window.document;
  const request = { id: "a", kind: "approval", status: "pending", approvalId: "request-a", approval: { kind: "write_file", files: ["src/app.ts"], preview: "--- a/src/app.ts\n+++ b/src/app.ts\n@@ -1 +1 @@\n-old\n+new\n+another" } };
  const chat = { sessionId: "one", items: [request], running: false };
  postState(window, chat);
  assert.equal(doc.querySelector("#reviewAttention").hidden, false);
  doc.querySelector("#reviewAttention").click();
  assert.equal(doc.activeElement.dataset.itemId, "a");
  assert.match(doc.querySelector(".diff-summary").getAttribute("aria-label"), /2 added lines, 1 removed line/);
  assert.equal(doc.querySelector(".diff-pre").tabIndex, 0);
  postState(window, { ...chat, items: [{ ...request, status: "allow_once" }] });
  assert.equal(doc.querySelector("#reviewAttention").hidden, true);
  const evidence = doc.querySelector(".approval-evidence");
  assert.equal(evidence.open, false);
  evidence.open = true;
  postState(window, { ...chat, items: [{ ...request, status: "allow_once", approval: { ...request.approval, reason: "Reviewed" } }] });
  assert.equal(doc.querySelector(".approval-evidence").open, true);
  assert.equal(doc.querySelectorAll(".approval-actions button").length, 0);
  assert.deepEqual(errors, []);
});

test("permission menu supports Escape and keyboard selection without changing the draft", () => {
  const { window, messages, errors } = mountStartView();
  const doc = window.document;
  postState(window, { sessionId: null, items: [], running: false });
  const input = doc.querySelector("#taskInput");
  input.value = "Keep my draft";
  doc.querySelector("#permissionStrip").click();
  assert.equal(doc.activeElement.getAttribute("data-permission"), "review");
  doc.querySelector("#permissionPalette").dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.equal(doc.activeElement.id, "permissionStrip");
  assert.equal(messages.some((m) => m.type === "cockpit" && m.message?.mode === "auto"), false);
  doc.querySelector("#permissionStrip").click();
  doc.querySelector("#permissionPalette").dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
  assert.equal(doc.activeElement.getAttribute("data-permission"), "auto");
  doc.activeElement.click();
  assert.equal(doc.querySelector("#taskMode").value, "review");
  assert.equal(messages.at(-1).message.mode, "auto");
  postState(window, { sessionId: null, items: [], running: false }, { mode: "auto" });
  assert.equal(doc.querySelector("#taskMode").value, "auto");
  assert.equal(input.value, "Keep my draft");
  assert.equal(doc.querySelector("#permissionPalette").hidden, true);
  postState(window, { sessionId: "one", mode: "review", items: [], running: true });
  assert.equal(doc.querySelector("#permissionStrip").disabled, true);
  assert.deepEqual(errors, []);
});

test("copy response sends original Markdown and is absent while streaming", () => {
  const { window, messages } = mountStartView();
  const item = { id: "reply", kind: "assistant", text: "**Verified** `src/app.ts`", status: "running" };
  postState(window, { sessionId: "one", items: [item], running: true });
  assert.equal(window.document.querySelector(".message-actions"), null);
  postState(window, { sessionId: "one", items: [{ ...item, status: "complete" }], running: false });
  window.document.querySelector(".message-actions button").click();
  assert.equal(messages.at(-1).type, "clipboard.copy");
  assert.equal(messages.at(-1).text, item.text);
  assert.match(window.document.querySelector(".message-actions").textContent, /Copied/);
});

test("IME confirmation does not accept a slash suggestion or send the task", () => {
  const { window, messages } = mountStartView();
  postState(window, { sessionId: null, items: [] }, { slashCommands: [{ command: "/forge plan", usage: "/forge plan <instruction>", takesArgs: true, description: "Plan" }] });
  const input = window.document.querySelector("#taskInput");
  input.value = "/p";
  input.dispatchEvent(new window.Event("input"));
  input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", isComposing: true, bubbles: true }));
  assert.equal(input.value, "/p");
  assert.equal(messages.some((m) => m.type === "task.submit"), false);
});

test("confirmed cancellation stops lingering activity without claiming it succeeded", () => {
  const { window } = mountStartView();
  const item = { id: "t", kind: "tool", toolName: "run_command", text: "Waiting for tests", status: "running" };
  postState(window, { sessionId: "one", running: true, jobStatus: "running", items: [item] });
  assert.ok(window.document.querySelector(".work-progress.running"));
  postState(window, { sessionId: "one", running: false, jobStatus: "cancelled", items: [item] });
  assert.equal(window.document.querySelector(".work-progress.running"), null);
  assert.match(window.document.querySelector(".work-progress-title").textContent, /Work stopped/);
  assert.equal(window.document.querySelector(".work-step-status").textContent, "Stopped");
  postState(window, { sessionId: "one", running: false, jobStatus: "needs_attention", items: [item] });
  assert.equal(window.document.querySelector("#sessionStatus").textContent, "Needs attention");
  assert.equal(window.document.querySelector(".work-step-status").textContent, "Ended");
});

test("session header offers only supported checkpoints and clears at a session boundary", () => {
  const { window, messages } = mountStartView();
  const doc = window.document;
  postState(window, { sessionId: "one", items: [{ id: "u", kind: "user", text: "Fix the header" }] }, { commands: [{ command: "alysis.backend.checkpoint.list", available: true }] });
  assert.equal(doc.querySelector("#sessionTitle").textContent, "Fix the header");
  assert.equal(doc.querySelector("#checkpointButton").hidden, false);
  doc.querySelector("#checkpointButton").click();
  assert.equal(messages.at(-1).command, "alysis.backend.checkpoint.list");
  postState(window, { sessionId: "one", items: [] }, { commands: [] });
  assert.equal(doc.querySelector("#checkpointButton").hidden, true);
  postState(window, { sessionId: null, items: [] });
  assert.equal(doc.querySelector("#sessionHeader").hidden, true);
});

test("dropped file mentions preserve full paths and handle percent signs without exceptions", () => {
  const { window, errors } = mountStartView();
  const input = window.document.querySelector("#taskInput");
  const drop = (uriList: string, files: any[] = []) => {
    const event = new window.Event("drop", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "dataTransfer", { value: { getData: () => uriList, files } });
    window.document.querySelector("#taskForm").dispatchEvent(event);
  };
  drop("file:///C:/work/project/src/deep/nested/app.ts");
  assert.equal(input.value, "@C:/work/project/src/deep/nested/app.ts ");
  input.value = "";
  drop("", [{ name: "100%complete.md", type: "text/plain" }]);
  assert.equal(input.value, "@100%complete.md ");
  input.value = "";
  drop("file:///repo/invalid%ZZ.ts");
  assert.equal(input.value, "");
  assert.deepEqual(errors, []);
});

test("late acceptance removes only the restored prompt and preserves newly typed context", () => {
  const { window, messages } = mountStartView();
  postState(window, { sessionId: null, items: [], running: false });
  const input = window.document.querySelector("#taskInput");
  input.value = "First instruction";
  input.dispatchEvent(new window.Event("input"));
  window.document.querySelector("#submitTask").click();
  const requestId = messages.find((message) => message.type === "task.submit").requestId;
  input.value = "New context typed while waiting";
  window.dispatchEvent(new window.MessageEvent("message", { data: { type: "task.result", started: false, requestId } }));
  assert.equal(input.value, "First instruction\n\nNew context typed while waiting");
  window.dispatchEvent(new window.MessageEvent("message", { data: { type: "task.result", started: true, requestId } }));
  assert.equal(input.value, "New context typed while waiting");
});

test("rejected submissions preserve an oversized combined draft through reload and require shortening before send", () => {
  const mounted = mountStartView();
  const { window, messages } = mounted;
  postState(window, { sessionId: null, items: [], running: false });
  const input = window.document.querySelector("#taskInput");
  input.value = "a".repeat(20000);
  input.dispatchEvent(new window.Event("input"));
  window.document.querySelector("#submitTask").click();
  const requestId = messages.find((message) => message.type === "task.submit").requestId;
  input.value = "b".repeat(20000);
  window.dispatchEvent(new window.MessageEvent("message", { data: { type: "task.result", started: false, requestId } }));
  const expected = "a".repeat(20000) + "\n\n" + "b".repeat(20000);
  assert.equal(input.value, expected);
  assert.equal(window.document.querySelector("#submitTask").disabled, true);
  input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  assert.equal(messages.filter((message) => message.type === "task.submit").length, 1);
  const reloaded = mountStartView(mounted.getPersistedState());
  assert.equal(reloaded.window.document.querySelector("#taskInput").value, expected);
  input.value = "Shortened prompt";
  input.dispatchEvent(new window.Event("input"));
  assert.equal(window.document.querySelector("#submitTask").disabled, false);
});

test("portable sidebar navigation preserves the draft, marks the current surface and routes new tasks to chat", () => {
  const { window, messages, errors } = mountStartView(undefined, true);
  const doc = window.document;
  postState(window, { sessionId: null, items: [], running: false }, { forgeEnabled: true });
  const input = doc.querySelector("#taskInput");
  input.value = "Keep this draft while I check settings";
  input.dispatchEvent(new window.Event("input"));
  const nav = doc.querySelector(".sidebar-top-bar");
  assert.equal(nav.hidden, false);
  nav.querySelector('[data-surface="settings"]').click();
  assert.equal(doc.querySelector("#settingsSurface").hidden, false);
  assert.equal(nav.querySelector('[aria-current="page"]').getAttribute("data-surface"), "settings");
  nav.querySelector('[data-surface="task"]').click();
  assert.equal(input.value, "Keep this draft while I check settings");
  assert.equal(doc.querySelector("#taskSurface").hidden, false);
  nav.querySelector('[data-surface="history"]').click();
  nav.querySelector('[data-action="new"]').click();
  assert.deepEqual(JSON.parse(JSON.stringify(messages.at(-1))), { type: "action", action: "new" });
  assert.equal(doc.querySelector("#taskSurface").hidden, false);
  assert.equal(nav.querySelectorAll('[aria-current="page"]').length, 1);
  postState(window, { sessionId: null, items: [], running: false }, { forgeEnabled: false });
  assert.equal(nav.querySelector('[data-surface="forge"]').hidden, true);
  assert.deepEqual(errors, []);
});

test("individual tool rows preserve their disclosure and keyboard focus when output streams", () => {
  const { window, errors } = mountStartView();
  const doc = window.document;
  const tool = { id: "streaming-tool", kind: "tool", toolName: "read_file", toolInput: '{"path":"src/app.ts"}', text: "First chunk", status: "running" };
  const chat = { sessionId: "one", items: [tool], running: true, jobStatus: "running" };
  postState(window, chat);
  doc.querySelector(".work-progress").open = true;
  const details = doc.querySelector(".work-step-technical");
  details.open = true;
  details.querySelector("summary").focus();
  postState(window, { ...chat, items: [{ ...tool, text: "First chunk\nSecond chunk" }] });
  assert.equal(doc.querySelector(".work-progress").open, true);
  assert.equal(doc.querySelector(".work-step-technical").open, true);
  assert.equal(doc.activeElement.dataset.focusKey, "work-technical:streaming-tool");
  assert.match(doc.querySelector(".work-step-technical pre").textContent, /Second chunk/);
  assert.equal(doc.querySelector(".work-step-technical pre").tabIndex, 0);
  assert.deepEqual(errors, []);
});

test("a review diff opens its host-supplied file without approving the proposed action", () => {
  const { window, messages, errors } = mountStartView();
  const item = { id: "review-file", kind: "approval", status: "pending", approvalId: "approve-file", approval: { kind: "write_file", files: ["src/app.ts"], preview: "--- a/src/app.ts\n+++ b/src/app.ts\n@@ -1 +1 @@\n-old\n+new" } };
  postState(window, { sessionId: "one", items: [item], running: false });
  window.document.querySelector(".diff-summary .file-mention").click();
  assert.deepEqual(JSON.parse(JSON.stringify(messages.at(-1))), { type: "open.file", path: "src/app.ts" });
  assert.equal(messages.some((message) => message.type === "approval"), false);
  assert.equal(window.document.querySelector(".approval-actions").children.length, 2);
  assert.deepEqual(errors, []);
});

test("every static Start View button contract stays wired after a state render", () => {
  const { window, messages, errors } = mountStartView();
  postState(window, {
    sessionId: null,
    mode: "review",
    jobStatus: "idle",
    running: false,
    items: []
  });
  const document = window.document;

  for (const button of document.querySelectorAll("button[data-action]")) {
    messages.length = 0;
    button.click();
    assert.equal(JSON.stringify(messages.at(-1)), JSON.stringify({ type: "action", action: button.dataset.action }));
  }
  for (const button of document.querySelectorAll("button[data-command]")) {
    messages.length = 0;
    button.click();
    assert.equal(JSON.stringify(messages.at(-1)), JSON.stringify({ type: "command", command: button.dataset.command }));
  }
  for (const button of document.querySelectorAll("button[data-surface]")) {
    button.click();
    assert.equal(document.querySelector(`#${button.dataset.surface}Surface`)?.hidden, false);
  }
  for (const button of document.querySelectorAll("button[data-cockpit-action]")) {
    messages.length = 0;
    button.click();
    assert.equal(messages.at(-1)?.type, "cockpit", button.dataset.cockpitAction);
    assert.equal(messages.at(-1)?.message?.type, button.dataset.cockpitAction);
  }
  for (const button of document.querySelectorAll("button[data-forge-tab]")) {
    button.click();
    assert.equal(document.querySelector(`[data-forge-panel="${button.dataset.forgeTab}"]`)?.hidden, false);
  }
  for (const button of document.querySelectorAll("button[data-starter]")) {
    button.click();
    assert.equal(document.querySelector("#taskInput").value, button.dataset.starter);
  }

  messages.length = 0;
  document.querySelector("[data-models-refresh]").click();
  assert.equal(JSON.stringify(messages.at(-1)), JSON.stringify({ type: "models.refresh" }));
  assert.deepEqual(errors, []);
});

test("Browser Cockpit separates agent-shared public browsing from confirmed Direct IDE loopback browsing", () => {
  const { window, messages, errors } = mountStartView();
  const document = window.document;
  window.dispatchEvent(new window.MessageEvent("message", {
    data: { type: "surface.show", surface: "browser" }
  }));
  window.dispatchEvent(new window.MessageEvent("message", {
    data: {
      type: "browser.state",
      state: {
        supported: true,
        reason: null,
        localTestingSupported: false,
        localTestingReason: "Direct IDE loopback browsing needs a newer CLI.",
        ownerSessionId: "owner-1",
        phase: "idle",
        sessions: [],
        selectedBrowserId: null,
        busy: null,
        notice: null,
        error: null,
        snapshot: null,
        screenshot: null,
        diagnostics: [],
        diagnosticsTruncated: false,
        previewUri: null,
        workspaceTrusted: true
      }
    }
  }));
  assert.equal(document.querySelector("#browserStart").disabled, false);
  assert.equal(document.querySelector("#browserStartLocal").disabled, true);
  assert.match(document.querySelector("#browserLocalHint").textContent, /newer CLI/);

  window.dispatchEvent(new window.MessageEvent("message", {
    data: {
      type: "browser.state",
      state: {
        supported: true,
        reason: null,
        localTestingSupported: true,
        localTestingReason: null,
        ownerSessionId: "owner-1",
        phase: "ready",
        sessions: [{
          browserSessionId: "browserSession1234567890",
          product: "Chromium",
          state: "running",
          createdAt: 1,
          networkScope: "public_loopback",
          activeUrl: "http://127.0.0.1:3000/",
          artifactCount: 0
        }],
        selectedBrowserId: "browserSession1234567890",
        busy: null,
        notice: null,
        error: null,
        snapshot: null,
        screenshot: null,
        diagnostics: [],
        diagnosticsTruncated: false,
        previewUri: null,
        workspaceTrusted: true
      }
    }
  }));
  assert.match(document.querySelector("#browserActorAccess").textContent, /Direct IDE only/);
  assert.match(document.querySelector("#browserActorAccess").textContent, /agent blocked/);
  assert.match(document.querySelector("#browserNetworkAccess").textContent, /loopback only/);

  messages.length = 0;
  document.querySelector("#browserStartLocal").click();
  assert.deepEqual(messages.map((message) => JSON.parse(JSON.stringify(message))), [{
    type: "cockpit",
    message: { type: "browser.startLocal" }
  }]);
  assert.doesNotMatch(JSON.stringify(messages), /allow_local_destinations|network_scope|confirm|yes/);
  assert.deepEqual(errors, []);
});

test("delegated conversation, history, and Forge controls route every dynamic action", () => {
  const { window, messages, errors } = mountStartView();
  window.dispatchEvent(new window.MessageEvent("message", {
    data: {
      type: "state",
      state: {
        mode: "review",
        engine: { tone: "ready", detail: "Connected" },
        workspace: { tone: "ready", detail: "Trusted" },
        provider: { tone: "ready", detail: "Configured" },
        conversation: {
          sessionId: "chat-session",
          mode: "review",
          jobStatus: "approval",
          running: true,
          items: [{
            id: "approval-1", kind: "approval", title: "Approve", text: "Run the check?",
            status: "pending", approvalId: "approval-1", allowForSession: true
          }]
        },
        recentTasks: [{ sessionId: "old-session", title: "Old task", detail: "Done", current: false, canResume: true }],
        forge: {
          sessionId: "forge-session",
          planId: "plan-1",
          activeJobId: null,
          planning: null,
          plan: {
            plan_id: "plan-1", session_id: "forge-session", status: "ready", project_goal: "Audit actions",
            summary: "", warnings: [],
            tasks: [{
              task_id: "task-1", title: "Review me", objective: "", status: "pending",
              file_scope: { estimated_files: [], write_scope: [] }, acceptance_criteria: [], verification_commands: []
            }]
          },
          events: [],
          diffs: [{ diff_id: "diff-1", file_path: "src/app.ts", status: "ready", size_bytes: 12 }],
          artifacts: [{ sessionId: "forge-session", artifacts: [{ artifact_id: "artifact-1", path: "report.txt", size_bytes: 8 }] }],
          executePreview: null,
          approvals: [{
            sessionId: "forge-session", approvalId: "forge-approval", reason: "Run tests",
            allowForSessionSupported: true
          }],
          review: null,
          reviewBusy: false,
          assets: [{ id: "asset-1", title: "Spec", kind: "File", sizeBytes: 4 }]
        },
        swarm: {
          supported: true, status: "running", busy: false, cancellable: true,
          tasks: [{ taskId: "swarm-1", title: "Worker", state: "review", reviewable: true, applied: false, discarded: false }],
          recovery: {
            supported: true,
            status: "ready",
            reason: null,
            activeJobId: null,
            jobs: [{
              jobId: "durable-job-1", state: "interrupted", revision: 7, attempts: 2, resumeCount: 1,
              updatedAt: 1_785_000_100, errorSummary: "Worker stopped safely.", calls: 3, totalTokens: 120
            }]
          }
        },
        actionResults: [],
        runtimeEvents: [],
        commands: []
      }
    }
  }));
  const document = window.document;

  messages.length = 0;
  for (const button of document.querySelectorAll("#conversationItems [data-approval-decision]")) {
    button.click();
  }
  assert.deepEqual(
    messages.map((message: any) => message.decision),
    ["allow_once", "allow_for_session", "deny"]
  );

  messages.length = 0;
  document.querySelector("#recentCards .task-card").click();
  assert.equal(JSON.stringify(messages.at(-1)), JSON.stringify({ type: "session", action: "resume", sessionId: "old-session" }));

  messages.length = 0;
  for (const button of document.querySelectorAll("#forgeWorkspace [data-dynamic-cockpit-action]")) {
    button.click();
  }
  const actions = messages
    .filter((message: any) => message.type === "cockpit")
    .map((message: any) => message.message.type);
  assert.deepEqual(actions.sort(), [
    "forge.approval", "forge.approval", "forge.approval", "forge.artifact.open", "forge.assets.open",
    "forge.diff.open", "forge.review", "swarm.apply", "swarm.cancel", "swarm.discard",
    "swarm.recovery.dismiss", "swarm.recovery.refresh", "swarm.recovery.resume"
  ].sort());
  const resumeMessage = messages.find((message: any) => message.message?.type === "swarm.recovery.resume");
  assert.equal(
    JSON.stringify(resumeMessage?.message),
    JSON.stringify({ type: "swarm.recovery.resume", jobId: "durable-job-1", revision: 7 })
  );
  assert.match(document.querySelector(".swarm-recovery-job")?.textContent ?? "", /Worker stopped safely/);

  messages.length = 0;
  document.querySelector("#forgeWorkspace [data-dynamic-command]").click();
  assert.equal(JSON.stringify(messages.at(-1)), JSON.stringify({ type: "command", command: "alysis.manageForgeAssets" }));
  assert.deepEqual(errors, []);
});

test("assistant Markdown is rendered with safe DOM nodes and no raw HTML injection", () => {
  const { window } = mountStartView();
  postState(window, {
    sessionId: "session-markdown",
    mode: "review",
    jobStatus: "completed",
    running: false,
    items: [
      {
        id: "a-markdown",
        kind: "assistant",
        title: "Alysis Code",
        text: "**Done** with `src/app.ts`.\n\n```ts\nconst ok = true;\n```\n\n<img src=x onerror=alert(1)>",
        status: "complete",
        approvalId: null
      }
    ]
  });

  const message = window.document.querySelector('[data-item-id="a-markdown"]');
  assert.equal(message.querySelector("strong").textContent, "Done");
  assert.equal(message.querySelector("pre code").textContent, "const ok = true;");
  assert.equal(message.querySelector("img"), null);
  assert.match(message.textContent, /<img src=x/);
});

test("task history is an in-view replacement surface, not another stacked sidebar view", () => {
  const { window } = mountStartView();
  window.dispatchEvent(new window.MessageEvent("message", {
    data: {
      type: "state",
      state: {
        mode: "review",
        providerName: "OpenAI",
        modelName: "gpt-5",
        engine: { tone: "ready", detail: "Connected" },
        workspace: { tone: "ready", detail: "Trusted" },
        provider: { tone: "ready", detail: "OpenAI · gpt-5" },
        recentTasks: [
          { sessionId: "session-1", title: "Task session-1", detail: "Review changes · Completed", current: false, canResume: true }
        ],
        conversation: { sessionId: null, mode: null, jobStatus: "idle", running: false, items: [] }
      }
    }
  }));

  const document = window.document;
  assert.equal(document.querySelector("#recentBlock").hidden, false);
  assert.equal(document.querySelectorAll("#recentCards .task-card").length, 1);
  document.querySelector('[data-surface="history"]').click();
  assert.equal(document.querySelector("#taskSurface").hidden, true);
  assert.equal(document.querySelector("#historySurface").hidden, false);
  assert.equal(document.body.classList.contains("secondary-open"), true);
});

test("Forge is a complete in-sidebar workspace with plan tasks and safe run actions", () => {
  const { window, messages } = mountStartView();
  window.dispatchEvent(new window.MessageEvent("message", {
    data: {
      type: "state",
      state: {
        mode: "review",
        engine: { tone: "ready", detail: "Connected" },
        workspace: { tone: "ready", detail: "Trusted" },
        provider: { tone: "ready", detail: "Configured" },
        conversation: { sessionId: null, mode: null, jobStatus: "idle", running: false, items: [] },
        forge: {
          sessionId: "forge-session",
          planId: "plan-123",
          activeJobId: null,
          planning: null,
          plan: {
            plan_id: "plan-123",
            session_id: "forge-session",
            status: "ready",
            project_goal: "Ship the new sidebar",
            summary: "Implement and verify the unified UI.",
            warnings: [],
            tasks: [{
              task_id: "task-1",
              title: "Build Forge surface",
              objective: "Expose Forge without another editor panel.",
              status: "pending",
              file_scope: { estimated_files: ["media/startView.html"], write_scope: [] },
              acceptance_criteria: ["Forge remains in the sidebar"],
              verification_commands: ["npm test"]
            }]
          },
          events: [],
          diffs: [],
          artifacts: [],
          executePreview: null,
          approvals: [],
          review: null,
          reviewBusy: false,
          assets: []
        },
        swarm: { supported: true, status: "idle", busy: false, cancellable: false, tasks: [] },
        actionResults: [],
        runtimeEvents: [],
        commands: []
      }
    }
  }));
  window.dispatchEvent(new window.MessageEvent("message", { data: { type: "surface.show", surface: "forge" } }));

  const document = window.document;
  assert.equal(document.querySelector("#forgeSurface").hidden, false);
  assert.equal(document.querySelector("#forgeWorkspace").hidden, false);
  assert.match(document.querySelector("#forgeSummary").textContent, /Ship the new sidebar/);
  assert.equal(document.querySelectorAll(".forge-task-card").length, 1);
  assert.equal(document.querySelector(".workflow-switch"), null, "workflow follows the active surface without a duplicate toggle");
  assert.equal(document.querySelector("#forgeEmpty .forge-logo img")?.tagName, "IMG");
  assert.equal(document.querySelector("#forgeEmpty .forge-orbit"), null, "the placeholder orbit is removed");

  document.querySelector('[data-cockpit-action="forge.executePreview"]').click();
  assert.equal(JSON.stringify(messages.at(-1)), JSON.stringify({ type: "cockpit", message: { type: "forge.executePreview", auto: true } }));
});

test("a swarm task shows the diff and the files that will not be applied before Apply", () => {
  const { window, messages, errors } = mountStartView();
  window.dispatchEvent(new window.MessageEvent("message", {
    data: {
      type: "state",
      state: {
        mode: "review",
        engine: { tone: "ready", detail: "Connected" },
        workspace: { tone: "ready", detail: "Trusted" },
        provider: { tone: "ready", detail: "Configured" },
        ready: true,
        readyReason: "Ready",
        conversation: { sessionId: null, mode: null, jobStatus: "idle", running: false, items: [] },
        forge: {
          sessionId: "forge-session",
          planId: "plan-9",
          activeJobId: null,
          planning: null,
          plan: {
            plan_id: "plan-9",
            session_id: "forge-session",
            status: "ready",
            project_goal: "Parallel work",
            summary: "",
            warnings: [],
            tasks: []
          },
          events: [],
          diffs: [],
          artifacts: [],
          executePreview: null,
          approvals: [],
          review: null,
          reviewBusy: false,
          assets: []
        },
        swarm: {
          supported: true,
          sessionId: "swarm-session",
          status: "review_pending",
          busy: false,
          cancellable: false,
          tasks: [{
            taskId: "task-7",
            title: "Rewrite the parser",
            state: "review_pending",
            reviewable: true,
            applied: false,
            discarded: false,
            diffAvailable: true,
            diffArtifactId: "artifact-diff-7",
            untrackedFilesNotApplied: ["scratch/notes.md", "scratch/tmp.json"],
            untrackedNote: "New files created by the worker stay outside the diff."
          }]
        },
        actionResults: [],
        runtimeEvents: [],
        commands: []
      }
    }
  }));
  window.dispatchEvent(new window.MessageEvent("message", { data: { type: "surface.show", surface: "forge" } }));

  const document = window.document;
  const task = document.querySelector(".swarm-task");
  assert.match(task.textContent, /2 new files will NOT be applied/);
  assert.match(task.textContent, /scratch\/notes\.md/);
  assert.match(task.textContent, /stay outside the diff/);

  const buttons = Array.from(task.querySelectorAll("button")) as any[];
  assert.deepEqual(buttons.map((button) => button.textContent), ["View diff", "Apply", "Discard"]);
  assert.ok(
    task.querySelector(".swarm-untracked").compareDocumentPosition(buttons[1])
      & window.Node.DOCUMENT_POSITION_FOLLOWING,
    "the review information comes before Apply"
  );

  messages.length = 0;
  buttons[0].click();
  assert.equal(
    JSON.stringify(messages.at(-1)),
    JSON.stringify({
      type: "cockpit",
      message: { type: "forge.artifact.open", sessionId: "swarm-session", artifactId: "artifact-diff-7" }
    })
  );
  assert.deepEqual(errors, []);
});

test("task and Forge onboarding use the bundled Alysis Code logo", () => {
  const { window } = mountStartView();
  const provider = readFileSync(resolve(ROOT, "src/views/StartViewProvider.ts"), "utf8");
  assert.match(provider, /resources", "alysis-logo\.png"/);
  assert.equal(window.document.querySelector(".welcome-mark img")?.tagName, "IMG");
  assert.equal(window.document.querySelector(".forge-logo img")?.tagName, "IMG");
});

test("the active surface routes Chat and Forge automatically", () => {
  const { window, messages } = mountStartView();
  const document = window.document;
  assert.equal(document.querySelector("#welcomeTitle")?.textContent, "Alysis Code");
  assert.equal(document.querySelector("[data-workflow]"), null);

  postState(window, { sessionId: null, mode: null, jobStatus: "idle", running: false, items: [] });

  document.querySelector("#taskInput").value = "Plan the release";
  window.dispatchEvent(new window.MessageEvent("message", { data: { type: "surface.show", surface: "forge" } }));
  document.querySelector("#taskForm").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  assert.equal(messages.at(-1)?.type, "task.submit");
  assert.equal(messages.at(-1)?.workflow, "forge");
});

test("slash button opens the slash-command menu and never runs VS Code commands from it", () => {
  const { window, messages } = mountStartView();
  window.dispatchEvent(new window.MessageEvent("message", {
    data: {
      type: "state",
      state: {
        mode: "review",
        engine: { tone: "ready", detail: "Connected" },
        workspace: { tone: "ready", detail: "Trusted" },
        provider: { tone: "ready", detail: "Configured" },
        ready: true,
        conversation: { sessionId: null, mode: null, jobStatus: "idle", running: false, items: [] },
        actionResults: [],
        runtimeEvents: [],
        // The VS Code command catalog is not what the "/" menu lists any more…
        commands: [{
          id: "alysis.showForge",
          command: "alysis.showForge",
          title: "Open Forge",
          description: "Plan and review complex work.",
          category: "Forge",
          mutates: false,
          available: true,
          unavailableReason: ""
        }],
        // …the real slash commands are.
        slashCommands: [
          { command: "/forge plan", title: "Forge Plan", description: "Create a Forge plan.", usage: "/forge plan <instruction>", takesArgs: true }
        ]
      }
    }
  }));

  window.document.querySelector(".slash-button").click();
  assert.equal(window.document.querySelector("#commandPalette").hidden, false);
  assert.equal(window.document.querySelectorAll(".command-row").length, 1);
  assert.equal(window.document.querySelector(".command-row code").textContent, "/forge plan");
  window.document.querySelector(".command-row").click();
  assert.equal(messages.some((message: any) => message.type === "command"), false, "a slash row inserts text; it never dispatches a host command");
  assert.equal(window.document.querySelector("#taskInput").value, "/forge plan ");
  assert.equal(window.document.querySelector("#commandPalette").hidden, true);
});

test("a structured approval renders as a question with a diff, then a muted resolved state", () => {
  const { window, messages, errors } = mountStartView();
  const document = window.document;
  const item = {
    id: "ap1",
    kind: "approval",
    title: "Approval needed",
    text: "Reason: review mode requires confirmation for write operations\nPreview: --- a/x",
    status: "pending",
    approvalId: "approval-1",
    allowForSession: true,
    approval: {
      kind: "write_file",
      reason: "review mode requires confirmation for write operations",
      preview: "--- a/SCRATCH.md\n+++ b/SCRATCH.md\n@@ -0,0 +1 @@\n+ui test",
      command: null,
      files: ["SCRATCH.md"],
      expiresAt: new Date(Date.now() + 5 * 60_000).toISOString(),
      scope: "exact file set (1 file)",
      warning: null
    }
  };
  postState(window, { sessionId: "s1", mode: "review", jobStatus: "running", running: true, items: [item] });

  const card = document.querySelector(".approval-card");
  assert.ok(card);
  assert.equal(card.classList.contains("pending"), true);
  assert.match(card.querySelector(".message-author").textContent, /Save changes to SCRATCH\.md\?/);
  assert.match(card.querySelector(".approval-expiry").textContent, /^Expires in /);
  assert.equal(card.querySelectorAll(".diff-line.diff-add").length, 1);
  assert.doesNotMatch(card.textContent, /Scope:|Expires: 20/);
  assert.deepEqual(
    Array.from(card.querySelectorAll(".approval-button")).map((button: any) => button.textContent),
    ["Save", "Allow for this task", "Deny"]
  );
  messages.length = 0;
  card.querySelector('[data-approval-decision="allow_once"]').click();
  assert.equal(JSON.stringify(messages.at(-1)), JSON.stringify({ type: "approval", approvalId: "approval-1", decision: "allow_once" }));

  postState(window, { sessionId: "s1", mode: "review", jobStatus: "running", running: true, items: [{ ...item, status: "allow_once", text: "Decision: allowed once." }] });
  const resolved = document.querySelector(".approval-card");
  assert.equal(resolved.classList.contains("resolved"), true);
  assert.match(resolved.querySelector(".message-author").textContent, /Approved$/);
  assert.equal(resolved.querySelectorAll(".approval-button").length, 0);
  assert.deepEqual(errors, []);
});

test("a command approval shows the command and offers Run", () => {
  const { window } = mountStartView();
  const document = window.document;
  postState(window, {
    sessionId: "s1", mode: "review", jobStatus: "running", running: true,
    items: [{
      id: "ap2", kind: "approval", title: "Approval needed", text: "Command: npm test", status: "pending", approvalId: "approval-2", allowForSession: false,
      approval: { kind: "shell", reason: "", preview: "", command: "npm test", files: [], expiresAt: null, scope: "", warning: null }
    }]
  });
  const card = document.querySelector(".approval-card");
  assert.match(card.querySelector(".message-author").textContent, /Run this command\?/);
  assert.match(card.querySelector(".code-block").textContent, /npm test/);
  assert.deepEqual(Array.from(card.querySelectorAll(".approval-button")).map((button: any) => button.textContent), ["Run command", "Deny"]);
});

function personaState(overrides: Record<string, unknown> = {}, conversation: Record<string, unknown> = {}): any {
  return {
    mode: "review",
    engine: { tone: "ready", detail: "Connected" },
    workspace: { tone: "ready", detail: "Trusted" },
    provider: { tone: "ready", detail: "Configured" },
    ready: true,
    readyReason: "Ready.",
    conversation: { sessionId: "session-1", mode: "review", jobStatus: "idle", running: false, items: [], ...conversation },
    personas: {
      supported: true,
      reason: null,
      enabled: true,
      active: "architect",
      activeSource: "user",
      options: [
        { name: "code", description: "Implements changes.", effectiveMode: "review", writeScoped: false, active: false },
        { name: "architect", description: "Writes markdown only.", effectiveMode: "review", writeScoped: true, active: true }
      ],
      ...overrides
    }
  };
}

test("the persona picker renders host-precomputed modes, posts persona.set, and the chip narrates", () => {
  const { window, messages, errors } = mountStartView();
  const document = window.document;
  window.dispatchEvent(new window.MessageEvent("message", { data: { type: "state", state: personaState() } }));

  const personaButton = document.querySelector("#personaButton");
  assert.equal(personaButton.hidden, false);
  assert.equal(personaButton.disabled, false);
  const chip = document.querySelector("#personaChip");
  assert.equal(chip.hidden, false, "the chip appears when the active persona is not code");
  assert.equal(chip.textContent, "architect · Review changes");
  assert.equal(chip.getAttribute("aria-label"), "Active persona: architect, Review changes");

  personaButton.click();
  assert.equal(document.querySelector("#personaPalette").hidden, false);
  const rows: any[] = Array.from(document.querySelectorAll("#personaList .command-row"));
  assert.equal(rows.length, 2);
  assert.match(rows[0].textContent, /Uses your permissions/, "inactive personas do not predict the mode restored by the agent");
  assert.match(rows[1].textContent, /architect/);
  assert.match(rows[1].textContent, /Review changes · limited writes · Current/, "rows show the precomputed clamped mode in the switch's own words");
  rows[0].click();
  const posted = messages.find((entry) => entry.type === "cockpit" && entry.message?.type === "persona.set");
  assert.equal(JSON.stringify(posted?.message), JSON.stringify({ type: "persona.set", name: "code" }));
  assert.equal(document.querySelector("#personaPalette").hidden, true);

  // "/persona arch" turns the slash palette into persona-name completion.
  const taskInput = document.querySelector("#taskInput");
  taskInput.value = "/persona arch";
  taskInput.dispatchEvent(new window.Event("input"));
  const completions: any[] = Array.from(document.querySelectorAll("#commandList .command-row"));
  assert.equal(completions.length, 1);
  assert.match(completions[0].textContent, /architect/);
  completions[0].click();
  const completed = messages.filter((entry) => entry.type === "cockpit" && entry.message?.type === "persona.set").at(-1);
  assert.equal(JSON.stringify(completed?.message), JSON.stringify({ type: "persona.set", name: "architect" }));
  assert.equal(taskInput.value, "", "completion clears the composer");
  assert.equal(errors.length, 0);
});

test("the persona control disables while running, shows the capability affordance, and honors the kill switch", () => {
  const { window } = mountStartView();
  const document = window.document;
  const personaButton = document.querySelector("#personaButton");
  const chip = document.querySelector("#personaChip");

  // Running: picker present but disabled — persona changes never queue behind a task.
  window.dispatchEvent(new window.MessageEvent("message", {
    data: { type: "state", state: personaState({}, { jobStatus: "running", running: true }) }
  }));
  assert.equal(personaButton.hidden, false);
  assert.equal(personaButton.disabled, true);
  assert.equal(personaButton.title, "Personas switch between tasks");

  // Older CLI: the standard capability affordance is shown instead of hiding the control.
  window.dispatchEvent(new window.MessageEvent("message", {
    data: {
      type: "state",
      state: personaState({
        supported: false,
        reason: "Needs a newer Alysis Code CLI — session.personas.list, session.persona.set",
        enabled: false,
        active: "",
        options: []
      })
    }
  }));
  assert.equal(personaButton.hidden, false);
  assert.equal(personaButton.disabled, true);
  assert.match(personaButton.title, /Needs a newer Alysis Code CLI/);
  assert.equal(chip.hidden, true);

  // CLI kill switch (enabled: false): hidden entirely.
  window.dispatchEvent(new window.MessageEvent("message", {
    data: { type: "state", state: personaState({ enabled: false, active: "", options: [] }) }
  }));
  assert.equal(personaButton.hidden, true);
  assert.equal(chip.hidden, true);
});
