/* Browser-only deterministic transport for ui-preview.js. */
(() => {
  const scenario = new URLSearchParams(location.search).get("scenario") || "welcome";
  let saved;
  let state = {
    gitWorkspace: { available: true, root: "/projects/example", branch: "main", isWorktree: false, additions: 17083, deletions: 1453, files: 24, untracked: 2, reason: "", canMove: !["welcome", "setup"].includes(scenario), busy: scenario === "streaming" },
    ready: true, readiness: { ok: true, blockers: [] }, mode: "review", providerName: "OpenAI", modelName: "gpt-5.6-sol",
    engine: { tone: "ready", detail: "Connected and ready" }, workspace: { tone: "ready", detail: "Trusted for reviewed changes" },
    provider: { tone: "ready", detail: "OpenAI · gpt-5.6-sol" },
    recentTasks: [
      { sessionId: "session-ui", title: "Fix the empty search state", detail: "Review changes · Completed · 2h ago", canResume: true },
      { sessionId: "session-tests", title: "Add coverage for the API client", detail: "Review changes · Completed · yesterday", canResume: true },
      { sessionId: "session-auth", title: "Understand the authentication flow", detail: "Read-only · Completed · 2d ago", canResume: true }
    ],
    conversation: { sessionId: null, mode: "review", running: false, jobStatus: "idle", items: [] },
    commands: [{ command: "alysis.backend.checkpoint.list", title: "Checkpoints", available: true }],
    slashCommands: [
      { command: "/permissions", description: "Choose what the agent may do", takesArgs: true, usage: "/permissions [readonly|review|auto]" },
      { command: "/subagents", description: "Inspect or toggle delegation settings", takesArgs: true, usage: "/subagents [status|on|off]" },
      { command: "/model", description: "Choose a model for this task", takesArgs: true, usage: "/model [name]" },
      { command: "/doctor", description: "Check your local setup", takesArgs: false, usage: "/doctor" }
    ],
    models: { supported: true, loaded: true, loading: false, activeProfile: "openai", activeModel: "gpt-5.6-sol", providers: [
      { key: "openai", label: "OpenAI", host: "api.openai.com", protocolKind: "native", protocolLabel: "Native API", models: ["gpt-5.6-sol", "gpt-5.4-mini"], profileName: "openai", connected: true, recommended: true, keyEnvVar: "OPENAI_API_KEY" },
      { key: "anthropic", label: "Anthropic", host: "api.anthropic.com", protocolKind: "native", protocolLabel: "Native API", models: ["claude-sonnet-4-6"], profileName: "anthropic", connected: false, recommended: true, keyEnvVar: "ANTHROPIC_API_KEY" },
      { key: "ollama", label: "Ollama", host: "localhost:11434", protocolKind: "compatibility", models: ["qwen3-coder"], profileName: "ollama", local: true, connected: false }
    ], connections: [{ profile: "openai", host: "api.openai.com", model: "gpt-5.6-sol", models: ["gpt-5.6-sol", "gpt-5.4-mini"], active: true, hasKey: true, keySource: "stored:profile=openai", storedInVsCode: true, protocolKind: "native" }] },
    personas: { supported: true, enabled: true, active: "code", options: [
      { name: "code", description: "Implement and verify focused changes", effectiveMode: "review", active: true },
      { name: "architect", description: "Design a solution and write markdown plans", permissionHint: "Limited writes, with review", effectiveMode: "review", active: false },
      { name: "ask", description: "Explain and investigate without changing files", permissionHint: "Read-only", effectiveMode: "readonly", active: false },
      { name: "debug", description: "Investigate and fix failures", permissionHint: "Uses your permissions", effectiveMode: "review", active: false }
    ] }
  };
  const items = [
    { id: "user-1", kind: "user", text: "Fix the search results so an empty query shows recent projects instead of a blank page." },
    { id: "assistant-1", kind: "assistant", status: "complete", text: "I’ll check the search flow and add a focused regression test." },
    { id: "tool-1", kind: "tool", title: "Read search component", toolName: "read_file", toolInput: '{"path":"src/components/Search.tsx"}', status: "complete", text: "Read 86 lines" },
    { id: "tool-2", kind: "tool", title: "Inspect search tests", toolName: "read_file", toolInput: '{"path":"src/components/Search.test.tsx"}', status: "complete", text: "Read 120 lines" },
    { id: "assistant-2", kind: "assistant", status: "complete", text: "The empty query returned before loading recent projects. The proposed change reuses the existing recent-projects list.\n\n- Empty searches show your **recent projects**.\n- Queries with no matches keep the “No results” message.\n- A regression test covers clearing the search field.\n\n```tsx\nconst results = query.trim()\n  ? searchProjects(query)\n  : recentProjects;\n```\n\nReview the change below before I save it." }
  ];
  const approval = {
    id: "approval-1", kind: "approval", status: "pending", approvalId: "request-1", allowForSession: true,
    approval: { kind: "write_file", reason: "review mode requires confirmation for write operations", files: ["src/components/Search.tsx"],
      preview: "--- a/src/components/Search.tsx\n+++ b/src/components/Search.tsx\n@@ -24,4 +24,5 @@\n export function SearchResults({ query }) {\n-  if (!query) return null;\n-  const results = searchProjects(query);\n+  const results = query.trim()\n+    ? searchProjects(query)\n+    : recentProjects;\n   return <ProjectList items={results} />;\n }", expiresAt: new Date(Date.now() + 300000).toISOString() }
  };
  if (scenario !== "welcome" && scenario !== "setup") {
    state.conversation = { sessionId: "session-ui", mode: "review", running: false, jobStatus: "completed", items: [...items, approval] };
    state.recentTasks[0].current = true;
  }
  if (scenario === "running") {
    state.conversation.running = true; state.conversation.jobStatus = "running";
    state.conversation.items = items.slice(0, 4);
    state.conversation.items.push({ id: "tool-3", kind: "tool", title: "Running tests", toolName: "run_command", toolInput: '{"command":"npm test -- Search"}', status: "running", text: "Running focused tests" });
  }
  if (["thinking", "thinking-start", "stopping", "streaming", "approval-running"].includes(scenario)) {
    state.conversation.running = true;
    state.conversation.jobStatus = scenario === "stopping" ? "cancellation_requested" : "running";
    state.conversation.items = scenario === "thinking-start" ? items.slice(0, 1) : items.slice(0, 4);
    if (scenario === "streaming") state.conversation.items.push({ ...items[4], status: "streaming" });
    if (scenario === "approval-running") state.conversation.items.push(approval);
  }
  if (scenario === "error") {
    state.conversation.jobStatus = "failed";
    state.conversation.items = [...items.slice(0, 2), { id: "error-1", kind: "error", errorKind: "network", errorTitle: "Connection interrupted", errorDetail: "Your conversation is saved. Check the connection and try again.", errorActions: [{ label: "Check connection", command: "alysis.showBridgeHealth", primary: true }] }];
  }
  if (scenario === "setup") {
    state.ready = false; state.readiness = { ok: false, blockers: [{ id: "provider", title: "Connect a model", detail: "Choose your provider to start working with Alysis Code.", actions: [{ label: "Choose a provider", command: "alysis.showModels" }] }] };
    state.recentTasks = [];
    state.models.connections = [];
    state.models.activeProfile = "";
    state.models.activeModel = "";
    state.modelName = "Choose a model";
    state.personas = null;
  }
  if (["approved", "denied", "expired"].includes(scenario)) {
    approval.status = scenario === "approved" ? "allow_once" : scenario === "denied" ? "deny" : "expired";
  }
  if (scenario === "history-empty") state.recentTasks = [];
  const showInitialSurface = () => {
    const target = scenario === "history-empty" ? "history" : scenario;
    if (["history", "models", "settings", "browser", "activity"].includes(target)) window.postMessage({ type: "surface.show", surface: target }, "*");
    if (target === "browser") window.postMessage({ type: "browser.state", state: { supported: true, localTestingSupported: true, phase: "idle", sessions: [], selectedBrowserId: null, workspaceTrusted: true } }, "*");
  };
  const publish = () => window.postMessage({ type: "state", state }, "*");
  window.acquireVsCodeApi = () => ({
    getState: () => saved, setState: (value) => { saved = value; },
    postMessage: (message) => {
      if (message.type === "ready") {
        state.commands.push(...Array.from(document.querySelectorAll("[data-command]"), (node) => ({ command: node.dataset.command, available: true })));
        publish();
        showInitialSurface();
      }
      if (message.type === "task.submit") {
        state.conversation = { sessionId: "session-ui", mode: message.mode, running: false, jobStatus: "completed", items: [{ id: `u-${Date.now()}`, kind: "user", text: message.instruction }, { id: `a-${Date.now()}`, kind: "assistant", text: "This local preview uses the production webview with a fixture bridge. Your message reached the transport successfully.", status: "complete" }] };
        state.lastAcceptedTaskRequestId = message.requestId;
        setTimeout(publish, 150);
      }
      if (message.type === "approval") { approval.status = message.decision; publish(); }
      if (message.type === "task.cancel") { state.conversation.running = false; state.conversation.jobStatus = "cancelled"; publish(); }
      if (message.type === "cockpit" && message.message.type === "mode.set") { state.mode = message.message.mode; state.conversation.mode = message.message.mode; publish(); }
      if (message.type === "mention.search") window.postMessage({ type: "mention.results", token: message.token, results: [{ label: "Search.tsx", detail: "src/components/Search.tsx", insert: "src/components/Search.tsx", kind: "file" }] }, "*");
      if (message.type === "models.refresh") publish();
      if (message.type === "model.set") { state.modelName = message.model; state.models.activeModel = message.model; state.models.connections[0].model = message.model; publish(); }
      if (message.type === "clipboard.copy") navigator.clipboard?.writeText(message.text).catch(() => {});
      if (message.type === "action" && message.action === "new") { state.conversation = { sessionId: null, mode: state.mode, running: false, jobStatus: "idle", items: [] }; publish(); }
    }
  });
})();
