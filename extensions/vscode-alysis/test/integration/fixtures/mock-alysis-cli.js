#!/usr/bin/env node

const readline = require("node:readline");
const { createHash } = require("node:crypto");

const PROTOCOL_VERSION = "1";
const SCENARIO = process.env.ALYSIS_TEST_MOCK_SCENARIO || "";
const EXPECTED_HOST_ACTIONS = [
  "tasks.list",
  "tasks.run",
  "tasks.terminate",
  "tasks.status",
  "debug.list",
  "debug.start",
  "debug.stop",
  "debug.status"
];
const REQUIRED_METHODS = [
  "initialize",
  "health",
  "getCapabilities",
  "session.create",
  "session.list",
  "session.cancel",
  "session.setModel",
  "session.modelInfo",
  "chat.send",
  "run.start",
  "job.status",
  "session.getEvents",
  "artifact.list",
  "artifact.read",
  "approval.respond",
  "host.action.respond",
  "profile.presets",
  "profile.preset",
  "profile.list",
  "profile.use",
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
  "tools.catalog",
  "tool.list",
  "tool.info",
  "skill.list",
  "skill.info",
  "mcp.status",
  "mcp.prompts.list",
  "hooks.list",
  "hooks.effective",
  "conventions.list",
  "conventions.render",
  "ext.list",
  "ext.info",
  "ext.search"
];

const MOCK_BROWSER_ID = "mock-browser-session-0001";
const MOCK_LOOPBACK_BROWSER_ID = "mock-loopback-session-0001";
const MOCK_BROWSER_ARTIFACT_ID = `browser:${MOCK_BROWSER_ID}:screenshot-0001-extension-host.png`;
// A valid 1x1 PNG followed by deterministic trailing bytes. The preview verifier deliberately
// validates the PNG signature, declared length, and SHA-256; the >1 MiB payload also forces the
// Extension Host path to exercise more than one browser.artifact.read chunk.
const MOCK_BROWSER_BASE_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64"
);
const MOCK_BROWSER_PNG = Buffer.concat([
  MOCK_BROWSER_BASE_PNG,
  Buffer.alloc((1024 * 1024) + 17, 0)
]);
const MOCK_BROWSER_PNG_SHA256 = createHash("sha256").update(MOCK_BROWSER_PNG).digest("hex");

const args = process.argv.slice(2);

if (args.length === 1 && args[0] === "--version") {
  if (SCENARIO === "version-failure") {
    process.stderr.write("mock alysis version failure\n");
    process.exit(2);
  }
  process.stdout.write("alysis 0.0.0-extension-host-test\n");
  process.exit(0);
}

if (args.join(" ") === "ide-bridge health") {
  if (SCENARIO === "health-broken") {
    process.stderr.write("mock bridge health failed before protocol startup\n");
    process.exit(3);
  }
  process.stdout.write(JSON.stringify(healthForScenario()) + "\n");
  process.exit(0);
}

if (args.join(" ") === "sandbox doctor --smoke") {
  if (SCENARIO === "doctor-failure") {
    process.stdout.write("doctor saw sk-test-secret-value while checking sandbox\n");
    process.stderr.write("authorization: Bearer abcdefghijklmnop\nmock sandbox doctor failed\n");
    process.exit(2);
  }
  process.stdout.write("sandbox doctor ok\n");
  process.exit(0);
}

if (args.join(" ") === "ide-bridge --stdio") {
  runStdioBridge();
} else {
  process.stderr.write(`unsupported mock alysis invocation: ${args.join(" ")}\n`);
  process.exit(2);
}

function runStdioBridge() {
  const sessions = new Map();
  const jobs = new Map();
  const browsers = new Map();
  const eventsBySession = new Map();
  let nextSequence = 1;
  let nextChatJobNumber = 1;
  const browserStatus = (entry) => ({
    browser_session_id: entry.browserSessionId,
    product: "Mock Chromium",
    state: "running",
    created_at: 0,
    allow_local_destinations: entry.networkScope !== "public",
    network_scope: entry.networkScope,
    active_url: entry.activeUrl,
    artifact_count: entry.artifactCount
  });
  const ownedBrowser = (params) => {
    const entry = browsers.get(params.browser_session_id);
    return entry && entry.ownerSessionId === params.session_id ? entry : null;
  };
  const emitEvent = (sequence, sessionId, jobId, type, payload) => {
    const event = {
      protocol_version: PROTOCOL_VERSION,
      session_id: sessionId,
      run_id: null,
      job_id: jobId,
      sequence,
      timestamp: new Date(0).toISOString(),
      type,
      payload
    };
    const events = eventsBySession.get(sessionId) || [];
    events.push(event);
    eventsBySession.set(sessionId, events);
    process.stdout.write(JSON.stringify(event) + "\n");
  };
  const rl = readline.createInterface({ input: process.stdin });
  rl.on("line", (line) => {
    if (!line.trim()) {
      return;
    }
    let request;
    try {
      request = JSON.parse(line);
    } catch {
      writeError(null, "invalid_json", "Request was not valid JSON.");
      return;
    }
    const params = request.params || {};
    switch (request.method) {
      case "initialize":
      case "health":
        writeResult(request.id, healthForScenario());
        return;
      case "getCapabilities":
        writeResult(request.id, healthForScenario().capabilities);
        return;
      case "profile.presets":
        writeResult(request.id, {
          presets: [{
            key: "mock-provider",
            label: "Mock Provider",
            base_url: "https://mock-provider.example/v1",
            base_url_host: "mock-provider.example",
            protocol_kind: "native",
            suggested_models: ["mock-profile-model"],
            key_env_var: "MOCK_PROVIDER_API_KEY"
          }],
          secret_values_included: false
        });
        return;
      case "profile.list":
        writeResult(request.id, {
          active_profile: "mock-profile",
          profiles: [{
            name: "mock-profile",
            base_url: "https://mock-provider.example/v1",
            base_url_host: "mock-provider.example",
            default_model: "mock-profile-model",
            protocol_kind: "native",
            api_key: { present: false, source: "fixture" },
            auth_provider: "",
            reasoning_effort: "",
            subscription_selection_ready: true
          }],
          secret_values_included: false
        });
        return;
      case "profile.preset":
        writeResult(request.id, { changed: true, profile: {}, secret_values_included: false });
        return;
      case "profile.use":
        writeResult(request.id, {
          active_profile: String(params.name || "mock-profile"),
          changed: true,
          profile: {},
          secret_values_included: false
        });
        return;
      case "session.create": {
        const sessionId = String(params.workspace || "").includes("-alysis-") ? `mock-worktree-${sessions.size}` : "mock-session";
        const session = mockSession(sessionId, params);
        const advertisedActions = params.host_capabilities?.actions;
        if (
          params.workspace_trusted !== true
          || params.host_capabilities?.protocol_version !== "1"
          || !Array.isArray(advertisedActions)
          || advertisedActions.length !== EXPECTED_HOST_ACTIONS.length
          || EXPECTED_HOST_ACTIONS.some((action, index) => advertisedActions[index] !== action)
        ) {
          writeError(request.id, "mock_host_capabilities_contract", "VS Code did not advertise the exact trusted host action set.");
          return;
        }
        const hostActions = {
          protocol_version: "1",
          actions: [...advertisedActions],
          workspace_fence: "wf_mock-extension-host",
          capability_fingerprint: "a".repeat(64),
          request_event: "host_action_requested",
          cancellation_event: "host_action_cancelled",
          session_closed_event: "session_closed",
          response_method: "host.action.respond",
          request_timeout_seconds: 30,
          max_argument_bytes: 8192,
          max_result_bytes: 65536
        };
        session.host_actions = hostActions;
        sessions.set(sessionId, session);
        writeResult(request.id, {
          session_id: session.session_id,
          workspace_root: session.workspace_root,
          mode: session.mode,
          host_actions: hostActions
        });
        return;
      }
      case "host.action.respond": {
        writeResult(request.id, {
          status: "applied",
          session_id: params.session_id,
          host_action_id: params.host_action_id,
          action: "tasks.list",
          outcome: params.ok ? "result" : "error"
        });
        return;
      }
      case "session.list":
        writeResult(request.id, { sessions: [...sessions.values()].map(sessionForList) });
        return;
      case "session.cancel": {
        const session = sessions.get(params.session_id);
        if (session) {
          session.closed = true;
          session.active_job = null;
        }
        for (const [browserId, browser] of browsers) {
          if (browser.ownerSessionId === params.session_id) {
            browsers.delete(browserId);
          }
        }
        writeResult(request.id, { session_id: params.session_id, status: "closed" });
        return;
      }
      case "session.setModel": {
        const sessionId = params.session_id || "mock-session";
        const session = sessions.get(sessionId) || mockSession(sessionId, params);
        session.model = typeof params.model === "string" && params.model.length > 0 ? params.model : session.model;
        sessions.set(sessionId, session);
        writeResult(request.id, sessionStatus(session));
        return;
      }
      case "session.modelInfo": {
        const sessionId = params.session_id || "mock-session";
        const session = sessions.get(sessionId) || mockSession(sessionId, params);
        sessions.set(sessionId, session);
        writeResult(request.id, sessionModelInfo(session, typeof params.model === "string" ? params.model : undefined));
        return;
      }
      case "chat.send":
      case "run.start": {
        // Mirror the real bridge: run.start on an EXISTING session accepts turn params only.
        // Session options belong to session.create; repeating them here is the contract
        // violation that once broke every first task against the real CLI while this mock
        // happily accepted it.
        if (request.method === "run.start" && typeof params.session_id === "string") {
          const sessionOptions = ["workspace", "mode", "model", "base_url", "temperature", "stream", "verify_cmd", "verify_commands", "subagents_enabled", "no_log", "yes", "max_steps", "active_workdir", "active_workdir_relpath", "workspace_trusted", "host_capabilities"]
            .filter((key) => key in params);
          if (sessionOptions.length > 0) {
            writeError(
              request.id,
              "unsupported_turn_option",
              "run.start with an existing session only accepts turn params; use session.setMode, session.setModel, session.setStream, or session.setActiveWorkdir for live session changes."
            );
            return;
          }
        }
        const sessionId = params.session_id || "mock-session";
        if (String(params.message || params.instruction || "").toLowerCase().includes("trigger bridge exit")) {
          process.nextTick(() => process.exit(7));
          return;
        }
        const job = {
          job_id: `mock-job-${nextChatJobNumber++}`,
          session_id: sessionId,
          status: "running",
          created_at: new Date(0).toISOString(),
          started_at: new Date(0).toISOString(),
          completed_at: null,
          exit_code: null,
          error: null
        };
        jobs.set(job.job_id, job);
        const session = sessions.get(sessionId);
        if (session) {
          session.active_job = job;
        }
        writeResult(request.id, { session_id: sessionId, job_id: job.job_id, status: "started" });
        const completeJob = () => {
          if (
            request.method === "run.start"
            && String(params.instruction || "").includes("Exercise the negotiated Extension Host task catalog")
            && session?.host_actions?.actions.includes("tasks.list")
          ) {
            const hostActions = session.host_actions;
            const hostActionId = `ha_${createHash("sha256").update(`extension-host:${sessionId}:${nextSequence}`).digest("hex").slice(0, 32)}`;
            emitEvent(nextSequence++, sessionId, job.job_id, "host_action_requested", {
              host_action_id: hostActionId,
              action: "tasks.list",
              arguments: {},
              workspace_root: session.workspace_root,
              workspace_fence: hostActions.workspace_fence,
              capability_fingerprint: hostActions.capability_fingerprint,
              expires_at: new Date(Date.now() + 30_000).toISOString(),
              protocol_version: "1",
              max_result_bytes: hostActions.max_result_bytes
            });
          }
          job.status = "completed";
          job.completed_at = new Date(0).toISOString();
          job.exit_code = 0;
          emitEvent(nextSequence++, sessionId, job.job_id, "message_end", { text: "mock response" });
        };
        if (String(params.message || params.instruction || "").includes("Fix the attached Extension Host diagnostic")) {
          setTimeout(completeJob, 750);
        } else {
          queueMicrotask(completeJob);
        }
        return;
      }
      case "job.status":
        writeResult(request.id, jobs.get(params.job_id) || {
          job_id: params.job_id || "mock-job",
          session_id: "mock-session",
          status: "completed",
          exit_code: 0,
          error: null
        });
        return;
      case "session.getEvents":
        const storedEvents = eventsBySession.get(params.session_id) || [];
        const afterSequence = typeof params.after_sequence === "number" ? params.after_sequence : 0;
        const maxEvents = Number(params.max_events || 500);
        const events = storedEvents.filter((event) => event.sequence > afterSequence).slice(0, maxEvents);
        writeResult(request.id, {
          session_id: params.session_id,
          events,
          truncated: false,
          lowest_retained_sequence: storedEvents.length > 0 ? storedEvents[0].sequence : null,
          highest_retained_sequence: storedEvents.length > 0 ? storedEvents[storedEvents.length - 1].sequence : null,
          max_events: maxEvents
        });
        return;
      case "artifact.list":
        const artifacts = [
          {
            artifact_id: "mock_run:output/result.json",
            root: "run",
            path: "output/result.json",
            size_bytes: 42
          }
        ];
        writeResult(request.id, {
          session_id: params.session_id,
          artifacts,
          truncated: false,
          max_items: 500,
          max_depth: 8
        });
        return;
      case "artifact.read":
        writeResult(request.id, {
          session_id: params.session_id,
          artifact_id: params.artifact_id,
          path: String(params.artifact_id || "mock_run:output/result.json"),
          size_bytes: 42,
          truncated: false,
          max_bytes: 65536,
          encoding: "utf-8",
          content: JSON.stringify({ status: "completed", fixture: "extension-host" }, null, 2)
        });
        return;
      case "approval.respond":
        writeResult(request.id, {
          session_id: params.session_id,
          approval_id: params.approval_id,
          status: "resolved",
          allow: params.allow === true,
          allow_for_session: false,
          allow_for_session_supported: false,
          allow_for_session_scope: null,
          allow_for_session_warning: null
        });
        queueMicrotask(() => {
          emitEvent(nextSequence++, params.session_id || "mock-session", null, "prompt_for_input", {
            kind: "approval_result",
            approval_id: params.approval_id,
            allow: params.allow === true,
            allow_for_session: params.allow_for_session === true
          });
        });
        return;
      case "browser.start": {
        const networkScope = String(params.network_scope || "");
        if (
          params.workspace_trusted !== true
          || !Object.prototype.hasOwnProperty.call(params, "network_scope")
          || Object.prototype.hasOwnProperty.call(params, "allow_local_destinations")
          || Object.prototype.hasOwnProperty.call(params, "executable_path")
          || !["public", "public_loopback"].includes(networkScope)
          || (networkScope === "public_loopback" && params.confirm !== true)
        ) {
          writeError(request.id, "mock_browser_contract", "Managed Browser must use an explicit trusted public or confirmed public-loopback scope without legacy local flags.");
          return;
        }
        const ownerSessionId = String(params.session_id || "");
        if (!sessions.has(ownerSessionId)) {
          writeError(request.id, "session_not_found", "Managed Browser owner session is unavailable.");
          return;
        }
        const entry = {
          browserSessionId: networkScope === "public_loopback" ? MOCK_LOOPBACK_BROWSER_ID : MOCK_BROWSER_ID,
          ownerSessionId,
          networkScope,
          activeUrl: null,
          artifactCount: 0
        };
        browsers.set(entry.browserSessionId, entry);
        writeResult(request.id, { session_id: ownerSessionId, ...browserStatus(entry) });
        return;
      }
      case "browser.navigate": {
        const entry = ownedBrowser(params);
        if (!entry || params.workspace_trusted !== true) {
          writeError(request.id, "browser_not_found", "Managed Browser navigation scope is invalid.");
          return;
        }
        const target = String(params.url || "https://example.test/");
        if (!mockBrowserDestinationAllowed(target, entry.networkScope)) {
          writeError(request.id, "browser_destination_denied", "Managed Browser destination is outside the selected network scope.");
          return;
        }
        entry.activeUrl = target;
        const data = { frame_id: "mock-main-frame", loader_id: "mock-loader" };
        writeResult(request.id, {
          session_id: entry.ownerSessionId,
          browser_session_id: entry.browserSessionId,
          url: entry.activeUrl,
          result: {
            data,
            truncated: false,
            size_bytes: Buffer.byteLength(JSON.stringify(data), "utf8")
          }
        });
        return;
      }
      case "browser.snapshot": {
        const entry = ownedBrowser(params);
        if (!entry) {
          writeError(request.id, "browser_not_found", "Managed Browser snapshot scope is invalid.");
          return;
        }
        const kind = params.kind || "text";
        const base = {
          session_id: entry.ownerSessionId,
          browser_session_id: entry.browserSessionId,
          kind,
          truncated: false
        };
        if (kind === "text") {
          const text = "Mock public page\nManaged Browser extension-host fixture";
          writeResult(request.id, { ...base, text, size_bytes: Buffer.byteLength(text, "utf8") });
          return;
        }
        const data = { role: "document", name: "Mock public page", children: [] };
        writeResult(request.id, {
          ...base,
          data,
          size_bytes: Buffer.byteLength(JSON.stringify(data), "utf8")
        });
        return;
      }
      case "browser.screenshot": {
        const entry = ownedBrowser(params);
        if (!entry) {
          writeError(request.id, "browser_not_found", "Managed Browser screenshot scope is invalid.");
          return;
        }
        entry.artifactCount += 1;
        writeResult(request.id, {
          session_id: entry.ownerSessionId,
          browser_session_id: entry.browserSessionId,
          artifact_id: MOCK_BROWSER_ARTIFACT_ID,
          media_type: "image/png",
          size_bytes: MOCK_BROWSER_PNG.length,
          sha256: MOCK_BROWSER_PNG_SHA256
        });
        return;
      }
      case "browser.artifact.read": {
        const entry = ownedBrowser(params);
        if (!entry || params.artifact_id !== MOCK_BROWSER_ARTIFACT_ID) {
          writeError(request.id, "artifact_not_found", "Managed Browser screenshot artifact is unavailable.");
          return;
        }
        const offset = Number.isSafeInteger(params.offset) && params.offset >= 0 ? params.offset : 0;
        const requested = Number.isSafeInteger(params.max_bytes) && params.max_bytes > 0
          ? params.max_bytes
          : 256 * 1024;
        const end = Math.min(MOCK_BROWSER_PNG.length, offset + requested);
        const chunk = MOCK_BROWSER_PNG.subarray(offset, end);
        writeResult(request.id, {
          session_id: entry.ownerSessionId,
          browser_session_id: entry.browserSessionId,
          artifact_id: MOCK_BROWSER_ARTIFACT_ID,
          media_type: "image/png",
          encoding: "base64",
          content: chunk.toString("base64"),
          offset,
          next_offset: end,
          size_bytes: MOCK_BROWSER_PNG.length,
          truncated: end < MOCK_BROWSER_PNG.length
        });
        return;
      }
      case "browser.diagnostics": {
        const entry = ownedBrowser(params);
        if (!entry) {
          writeError(request.id, "browser_not_found", "Managed Browser diagnostics scope is invalid.");
          return;
        }
        const allEvents = [
          {
            category: "console",
            method: "Runtime.consoleAPICalled",
            params: { data: { type: "log", text: "mock console event" }, truncated: false, size_bytes: 42 }
          },
          {
            category: "network",
            method: "Network.responseReceived",
            params: { data: { status: 200, host: "example.test" }, truncated: false, size_bytes: 36 }
          }
        ];
        const maxEvents = Number.isSafeInteger(params.max_events) && params.max_events >= 0
          ? params.max_events
          : 100;
        const events = allEvents.slice(0, maxEvents);
        writeResult(request.id, {
          session_id: entry.ownerSessionId,
          browser_session_id: entry.browserSessionId,
          events,
          truncated: events.length < allEvents.length,
          max_events: maxEvents
        });
        return;
      }
      case "browser.click": {
        const entry = ownedBrowser(params);
        if (!entry || params.workspace_trusted !== true) {
          writeError(request.id, "browser_not_found", "Managed Browser click scope is invalid.");
          return;
        }
        writeResult(request.id, {
          session_id: entry.ownerSessionId,
          browser_session_id: entry.browserSessionId,
          clicked: true
        });
        return;
      }
      case "browser.type": {
        const entry = ownedBrowser(params);
        if (!entry || params.workspace_trusted !== true) {
          writeError(request.id, "browser_not_found", "Managed Browser type scope is invalid.");
          return;
        }
        writeResult(request.id, {
          session_id: entry.ownerSessionId,
          browser_session_id: entry.browserSessionId,
          typed: true,
          character_count: String(params.text || "").length
        });
        return;
      }
      case "browser.status": {
        const entry = ownedBrowser(params);
        if (!entry) {
          writeError(request.id, "browser_not_found", "Managed Browser status scope is invalid.");
          return;
        }
        writeResult(request.id, { session_id: entry.ownerSessionId, ...browserStatus(entry) });
        return;
      }
      case "browser.list": {
        const ownerSessionId = String(params.session_id || "");
        const owned = [...browsers.values()].filter((entry) => entry.ownerSessionId === ownerSessionId);
        writeResult(request.id, {
          session_id: ownerSessionId,
          browsers: owned.map(browserStatus),
          count: owned.length
        });
        return;
      }
      case "browser.close": {
        const entry = ownedBrowser(params);
        if (params.delete_artifacts !== true || params.confirm !== true) {
          writeError(request.id, "confirmation_required", "Managed Browser cleanup requires explicit confirmation and artifact deletion.");
          return;
        }
        if (entry) {
          browsers.delete(entry.browserSessionId);
        }
        writeResult(request.id, {
          session_id: String(params.session_id || ""),
          browser_session_id: String(params.browser_session_id || ""),
          status: entry ? "closed" : "not_found"
        });
        return;
      }
      default:
        writeError(request.id, "method_not_found", `Unknown method: ${request.method}`);
    }
  });
}

function healthForScenario() {
  if (SCENARIO === "health-missing-methods") {
    const broken = health(REQUIRED_METHODS.filter((method) => method !== "artifact.read"));
    broken.capabilities.features.mock_scenario = SCENARIO;
    return broken;
  }
  const current = health(REQUIRED_METHODS);
  current.capabilities.features.mock_scenario = SCENARIO || "default";
  return current;
}

function mockBrowserDestinationAllowed(value, networkScope) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) {
    return false;
  }
  const host = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, "");
  const loopback = host === "localhost"
    || host.endsWith(".localhost")
    || host === "::1"
    || /^127(?:\.\d{1,3}){3}$/.test(host);
  const ipv4 = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  const octets = ipv4 ? ipv4.slice(1).map(Number) : [];
  const privateOrLinkLocal = host.endsWith(".local")
    || host === "0.0.0.0"
    || host === "::"
    || host.startsWith("fe8")
    || host.startsWith("fe9")
    || host.startsWith("fea")
    || host.startsWith("feb")
    || host.startsWith("fc")
    || host.startsWith("fd")
    || (octets.length === 4 && (
      octets[0] === 10
      || (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31)
      || (octets[0] === 192 && octets[1] === 168)
      || (octets[0] === 169 && octets[1] === 254)
    ));
  if (loopback) {
    return networkScope === "public_loopback";
  }
  return !privateOrLinkLocal;
}

function health(methods) {
  return {
    ok: true,
    name: "alysis-ide-bridge",
    alysis_version: "0.0.0-extension-host-test",
    protocol_version: PROTOCOL_VERSION,
    capabilities: {
      protocol_version: PROTOCOL_VERSION,
      methods,
      events: ["message_delta", "message_end", "prompt_for_input", "host_action_requested", "host_action_cancelled", "session_closed"],
      modes: ["readonly", "review", "auto"],
      transport: "stdio-jsonl",
      features: {
        structured_surface_events: true,
        terminal_output_scraping: false,
        secret_redaction: true,
        host_actions: {
          protocol_version: "1",
          events: ["host_action_requested", "host_action_cancelled", "session_closed"]
        },
        managed_browser: {
          supported: true,
          owned_chromium_processes: true,
          loopback_cdp_only: true,
          private_profiles_outside_workspaces: true,
          guarded_navigation: true,
          redirect_and_subresource_interception: true,
          persistent_child_target_interception: true,
          validating_egress_proxy: true,
          dns_resolution_pinned_to_numeric_connect: true,
          no_direct_network_fallback: true,
          loopback_proxy_bypass_removed: true,
          non_proxied_udp_disabled: true,
          public_destinations_by_default: true,
          local_destinations_require_confirmation: true,
          direct_loopback_scope: true,
          direct_loopback_scope_agent_isolated: true,
          direct_loopback_scope_denies_lan_and_link_local: true,
          bounded_snapshots: true,
          chunked_screenshot_artifacts: true,
          bounded_diagnostics: true,
          session_cleanup: true
        },
        approvals: {
          host_managed: true,
          allow_for_session: "scoped",
          allow_for_session_scope_required: true
        }
      }
    }
  };
}

function writeResult(id, result) {
  process.stdout.write(JSON.stringify({ protocol_version: PROTOCOL_VERSION, id, ok: true, result }) + "\n");
}

function writeError(id, code, message) {
  process.stdout.write(JSON.stringify({ protocol_version: PROTOCOL_VERSION, id, ok: false, error: { code, message } }) + "\n");
}

function mockSession(sessionId, params) {
  return {
    session_id: sessionId,
    workspace_root: typeof params.workspace === "string" ? params.workspace : "",
    mode: params.mode === "auto" || params.mode === "review" ? params.mode : "readonly",
    model: typeof params.model === "string" && params.model.length > 0 ? params.model : "mock-profile-model",
    provider: "mock-provider",
    profile: "mock-profile",
    closed: false,
    active_job: null
  };
}

function sessionModelInfo(session, requestedModel) {
  return {
    session_id: session.session_id,
    model: requestedModel && requestedModel.length > 0 ? requestedModel : session.model,
    provider: session.provider,
    profile: session.profile,
    base_url: "https://mock-provider.example/v1",
    base_url_redacted: false,
    context_window: 128000,
    vision_support: false,
    tool_support: true,
    streaming_support: true,
    source: "profile",
    source_metadata: { fixture: "extension-host" },
    secret_values_included: false
  };
}

function sessionStatus(session) {
  return {
    session_id: session.session_id,
    workspace_root: session.workspace_root,
    mode: session.mode,
    closed: session.closed,
    active_job: session.active_job ? { ...session.active_job } : null,
    model: session.model,
    base_url: "https://mock-provider.example/v1",
    stream: true,
    max_steps: 20,
    no_log: false,
    yes: false,
    subagents_enabled: false,
    active_workdir: null
  };
}

function sessionForList(session) {
  return {
    ...session,
    active_job: session.active_job ? { ...session.active_job } : null
  };
}
