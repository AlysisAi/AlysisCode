import assert from "node:assert/strict";
import test from "node:test";

import { CockpitRuntimeState, cliHealthFromFailureCode } from "../src/chat/CockpitRuntimeState";
import { PROTOCOL_VERSION, REQUIRED_BRIDGE_METHODS } from "../src/client/AlysisProtocol";
import type { AlysisConfig } from "../src/client/CliDiscovery";

test("CockpitRuntimeState keeps CLI origin separate from broken CLI health", () => {
  const runtime = new CockpitRuntimeState();
  runtime.applyConfig(configWithCli({ executionAllowed: true }));
  runtime.applyDetectionResult({
    ok: false,
    cliPath: "alysis",
    code: "health_nonzero",
    message: "Alysis Code bridge health exited with code 2."
  });

  const snapshot = runtime.snapshot();
  assert.equal(snapshot.cliOrigin.status, "trusted");
  assert.equal(snapshot.cliHealth.status, "broken");
  assert.equal(snapshot.bridgeProcess.status, "error");
});

test("CockpitRuntimeState publishes bounded managed-runtime provenance without an executable path", () => {
  const runtime = new CockpitRuntimeState();
  const config = configWithCli({ executionAllowed: true });
  config.runtimeSelection = {
    origin: "managed",
    production: true,
    executablePath: "C:\\private\\managed\\alysis.exe",
    artifactVersion: "runtime-1",
    cliVersion: "0.1.4",
    protocolVersion: "1",
    message: "Verified managed CLI 0.1.4 (runtime-1)."
  };

  runtime.applyConfig(config);

  assert.deepEqual(runtime.snapshot().runtimeSelection, {
    origin: "managed",
    production: true,
    message: "Verified managed CLI 0.1.4 (runtime-1)."
  });
  assert.equal("executablePath" in runtime.snapshot().runtimeSelection, false);
});

test("CockpitRuntimeState clears validation progress even when the other runtime fields are unchanged", () => {
  const runtime = new CockpitRuntimeState();
  const config = configWithCli({ executionAllowed: false });
  config.runtimeSelection = {
    origin: "unavailable", production: false, message: "Preparing runtime.", validating: true
  };
  runtime.applyConfig(config);
  assert.equal(runtime.snapshot().runtimeSelection.validating, true);
  config.runtimeSelection = { ...config.runtimeSelection, validating: false };
  runtime.applyConfig(config);
  assert.equal(runtime.snapshot().runtimeSelection.validating, undefined);
});

test("CockpitRuntimeState does not keep stale CLI health green when origin becomes blocked", () => {
  const runtime = new CockpitRuntimeState();

  runtime.applyConfig(configWithCli({ executionAllowed: true }));
  runtime.applyDetectionResult({
    ok: true,
    cliPath: "alysis",
    versionOutput: "Alysis Code 1.0.0",
    health: {
      ok: true,
      name: "alysis",
      protocol_version: "1",
      alysis_version: "1.0.0",
      capabilities: {
        protocol_version: "1",
        methods: [],
        events: [],
        modes: ["readonly"],
        transport: "stdio"
      }
    }
  });
  runtime.applyConfig(configWithCli({ executionAllowed: false, reason: "Workspace CLI path is blocked." }));

  const snapshot = runtime.snapshot();
  assert.equal(snapshot.cliOrigin.status, "blocked");
  assert.equal(snapshot.cliHealth.status, "unknown");
});

test("CockpitRuntimeState maps status bar error into bridge process error", () => {
  const runtime = new CockpitRuntimeState();

  runtime.applyStatusBarSnapshot({
    state: "error",
    text: "$(error) Alysis Code: Error",
    tooltip: "bridge failed",
    visible: true
  });

  assert.equal(runtime.snapshot().bridgeProcess.status, "error");
  assert.equal(runtime.snapshot().bridgeProcess.message, "bridge failed");
});

test("CockpitRuntimeState clears stale protocol health when CLI is missing", () => {
  const runtime = new CockpitRuntimeState();

  runtime.applyStatusBarSnapshot({
    state: "bridgeOk",
    text: "$(check) Alysis Code: bridge ok",
    tooltip: "Alysis Code IDE bridge health is ok",
    visible: true
  });
  runtime.applyStatusBarSnapshot({
    state: "missingCli",
    text: "$(warning) Alysis Code: missing CLI",
    tooltip: "Configure the Alysis Code CLI path",
    visible: true
  });

  const snapshot = runtime.snapshot();
  // FE-10: a not-found CLI is "unreachable" (Locate CLI), distinct from the version-gap "incompatible".
  assert.equal(snapshot.cliHealth.status, "unreachable");
  assert.equal(snapshot.bridgeProcess.status, "stopped");
  assert.equal(snapshot.bridgeProtocol.status, "unknown");
});

test("CockpitRuntimeState stores feature compatibility from bridge health", () => {
  const runtime = new CockpitRuntimeState();

  runtime.applyDetectionResult({
    ok: true,
    cliPath: "alysis",
    versionOutput: "Alysis Code 0.1.4",
    health: {
      ok: true,
      name: "alysis",
      protocol_version: PROTOCOL_VERSION,
      alysis_version: "0.1.4",
      capabilities: {
        protocol_version: PROTOCOL_VERSION,
        methods: [...REQUIRED_BRIDGE_METHODS, "session.create", "chat.send"],
        events: [],
        modes: ["readonly"],
        transport: "stdio-jsonl"
      }
    }
  });

  const snapshot = runtime.snapshot();
  assert.equal(snapshot.compatibility.features.baselineCockpit.supported, true);
  assert.equal(snapshot.compatibility.features.chatRun.supported, false);
  assert.equal(snapshot.compatibility.installCommand, "pipx install alysis-code");
});

test("CockpitRuntimeState applies bridge health as the Manage capability source of truth", () => {
  const runtime = new CockpitRuntimeState();

  runtime.applyBridgeHealth(fullManageHealth());

  const snapshot = runtime.snapshot();
  assert.equal(snapshot.compatibility.features.tools.supported, true);
  assert.equal(snapshot.compatibility.features.skills.supported, true);
  assert.equal(snapshot.compatibility.features.mcp.supported, true);
  assert.equal(snapshot.compatibility.features.hooks.supported, true);
  assert.equal(snapshot.compatibility.features.conventions.supported, true);
  assert.equal(snapshot.compatibility.features.ext.supported, true);
});

test("CockpitRuntimeState bridgeOk status bar transition preserves checked compatibility", () => {
  const runtime = new CockpitRuntimeState();
  runtime.applyBridgeHealth(fullManageHealth());

  runtime.applyStatusBarSnapshot({
    state: "bridgeOk",
    text: "$(check) Alysis Code: bridge ok",
    tooltip: "Alysis Code IDE bridge health is ok",
    visible: true
  });

  const checked = runtime.snapshot();
  assert.equal(checked.bridgeProcess.status, "ready");
  assert.equal(checked.bridgeProtocol.status, "ok");
  assert.equal(checked.compatibility.features.tools.supported, true);
  assert.equal(checked.compatibility.features.ext.supported, true);
});

test("CockpitRuntimeState bridgeOk status bar transition does not create ready state without health", () => {
  const runtime = new CockpitRuntimeState();

  runtime.applyStatusBarSnapshot({
    state: "bridgeOk",
    text: "$(check) Alysis Code: bridge ok",
    tooltip: "Alysis Code IDE bridge health is ok",
    visible: true
  });

  const snapshot = runtime.snapshot();
  assert.equal(snapshot.bridgeProcess.status, "stopped");
  assert.equal(snapshot.bridgeProtocol.status, "unknown");
  assert.equal(snapshot.compatibility.features.tools.supported, false);
});

function configWithCli(options: { executionAllowed: boolean; reason?: string }): AlysisConfig {
  return {
    cliPath: "alysis",
    defaultMode: "readonly",
    defaultModel: "",
    baseUrl: "",
    provider: "",
    transport: "stdio",
    sandboxProfile: "default",
    forgeExecuteMaxSteps: undefined,
    forgeExecuteNoLog: false,
    showStatusBar: true,
    autoStartBridge: false,
    enableForge: true,
    security: {
      isWorkspaceTrusted: true,
      ignoredWorkspaceSettings: [],
      workspaceRoots: ["/workspace/project"],
      cliPath: {
        value: "alysis",
        source: "default",
        trusted: options.executionAllowed,
        executionAllowed: options.executionAllowed,
        reason: options.reason,
        apiKeyForwardingAllowed: options.executionAllowed
      }
    }
  };
}

function fullManageHealth() {
  return {
    ok: true,
    name: "alysis",
    protocol_version: PROTOCOL_VERSION,
    alysis_version: "0.1.4",
    capabilities: {
      protocol_version: PROTOCOL_VERSION,
      methods: [
        ...REQUIRED_BRIDGE_METHODS,
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
      ],
      events: [],
      modes: ["readonly"],
      transport: "stdio-jsonl"
    }
  };
}

test("FE-10: cliHealthFromFailureCode maps each detection code to the right recovery taxonomy", () => {
  // Can't find / launch / reach the CLI -> "unreachable" (Locate, not Upgrade).
  for (const code of ["cli_missing", "cli_nonzero", "cli_error", "unsupported_transport"]) {
    assert.equal(cliHealthFromFailureCode(code), "unreachable", code);
  }
  // A real CLI responded but is too old -> "incompatible" (Upgrade).
  for (const code of ["unsupported_protocol_version", "incompatible_bridge", "ide_bridge_missing"]) {
    assert.equal(cliHealthFromFailureCode(code), "incompatible", code);
  }
  // It launched but the bridge returned bad/unhealthy data -> "broken".
  for (const code of ["health_nonzero", "invalid_json", "invalid_health", "bridge_unhealthy"]) {
    assert.equal(cliHealthFromFailureCode(code), "broken", code);
  }
  // Lock the FE-10 reclassification: a transport failure is NOT a version gap.
  assert.notEqual(cliHealthFromFailureCode("unsupported_transport"), "incompatible");
});

function makeHealth(methods: string[], modes: string[] = ["readonly", "review", "auto"]): any {
  return {
    ok: true,
    name: "alysis",
    protocol_version: PROTOCOL_VERSION,
    alysis_version: "0.1.4",
    capabilities: {
      protocol_version: PROTOCOL_VERSION,
      methods: [...new Set(["initialize", "health", "getCapabilities", ...methods])],
      events: [],
      modes,
      transport: "stdio-jsonl"
    }
  };
}

const idleSnapshot = {
  state: "idle" as const,
  text: "$(symbol-method) Alysis Code: idle",
  tooltip: "Alysis Code is ready",
  visible: true
};

test("FE-19: a live healthy bridge populates compatibility on an idle status bar (no detection probe)", () => {
  const runtime = new CockpitRuntimeState();
  // Cold start: empty compatibility, idle/unknown/stopped — the stale state that read "needs newer CLI".
  assert.equal(runtime.snapshot().compatibility.features.tools.supported, false);

  // The status bar is the resting "idle" — but the bridge is live + healthy and serving requests.
  const health = makeHealth(["tools.catalog", "tool.list", "tool.info"]);
  runtime.applyStatusBarSnapshot(idleSnapshot, { running: true, health });

  const snap = runtime.snapshot();
  // Compatibility is now LIVE: the supported domain un-gates from the advertised methods.
  assert.equal(snap.compatibility.features.tools.supported, true, "tools un-gated from live capabilities");
  assert.equal(snap.compatibility.features.baselineCockpit.supported, true);
  // The drawer/status fields agree with a connected bridge: healthy / connected / ok — never unknown/stopped.
  assert.equal(snap.cliHealth.status, "ok", "CLI health reads connected, not unknown");
  assert.equal(snap.bridgeProcess.status, "ready", "bridge reads ready, not stopped");
  assert.equal(snap.bridgeProtocol.status, "ok");
  // A genuinely-missing capability still gates, with the specific missing methods named.
  assert.equal(snap.compatibility.features.skills.supported, false);
  assert.ok(snap.compatibility.features.skills.missingMethods.length > 0, "missing methods are named");
});

test("FE-19: an idle status bar with no live bridge stays honestly stopped + gated", () => {
  const runtime = new CockpitRuntimeState();
  runtime.applyStatusBarSnapshot(idleSnapshot); // no live arg -> bridge not running
  const snap = runtime.snapshot();
  assert.equal(snap.bridgeProcess.status, "stopped");
  assert.equal(snap.compatibility.features.tools.supported, false, "no live bridge -> honestly gated");
});

test("FE-19: permanently-gated features stay gated even when the live bridge advertises their methods", () => {
  const runtime = new CockpitRuntimeState();
  // A bridge that (hypothetically) advertises EVERYTHING, including the permanently-gated method names.
  const health = makeHealth(
    ["forge.pr", "forge.swarm", "diff.apply", "diff.discard"],
    ["readonly", "review", "auto", "fullaccess"]
  );
  runtime.applyStatusBarSnapshot(idleSnapshot, { running: true, health });
  const features = runtime.snapshot().compatibility.features;
  assert.equal(features.forgePr.supported, false, "forgePr stays permanently gated");
  assert.equal(features.forgeSwarm.supported, false, "forgeSwarm stays permanently gated");
  assert.equal(features.diffApplyDiscard.supported, false, "diffApplyDiscard stays permanently gated");
});

test("FE-19: a live healthy bridge overrides a flaky 'missing CLI' probe (no false re-gating)", () => {
  const runtime = new CockpitRuntimeState();
  const health = makeHealth(["tools.catalog", "tool.list", "tool.info"]);
  runtime.applyStatusBarSnapshot(idleSnapshot, { running: true, health });
  assert.equal(runtime.snapshot().compatibility.features.tools.supported, true);

  // The separate out-of-band detection probe transiently reports "missing CLI" while the bridge is live.
  runtime.applyStatusBarSnapshot(
    { state: "missingCli", text: "x", tooltip: "Configure the Alysis Code CLI path", visible: true },
    { running: true, health }
  );
  const snap = runtime.snapshot();
  // The live, handshaken bridge is authoritative — it keeps its capabilities and reads connected.
  assert.equal(snap.compatibility.features.tools.supported, true, "live bridge keeps capabilities");
  assert.equal(snap.cliHealth.status, "ok");
  assert.equal(snap.bridgeProcess.status, "ready");
});

test("FE-19: a blocked-origin CLI is never un-gated from a stale running process", () => {
  const runtime = new CockpitRuntimeState();
  // The executable-origin / Workspace-Trust guard blocks the CLI.
  runtime.setCliOrigin("blocked", "CLI execution is blocked by executable-origin guards.");
  // A still-running (origin-trusted-at-spawn) process must NOT re-populate compatibility on the next sync.
  const health = makeHealth(["tools.catalog", "tool.list", "tool.info"]);
  runtime.applyStatusBarSnapshot(idleSnapshot, { running: true, health });
  const snap = runtime.snapshot();
  assert.equal(snap.cliOrigin.status, "blocked");
  assert.equal(snap.compatibility.features.tools.supported, false, "a blocked origin stays gated, not re-populated");
});

test("FE-19: re-applying identical live health does not re-publish (no emit storm)", () => {
  const runtime = new CockpitRuntimeState();
  const health = makeHealth(["tools.catalog", "tool.list", "tool.info"]);
  runtime.applyStatusBarSnapshot(idleSnapshot, { running: true, health });
  let emits = 0;
  runtime.onDidChange(() => {
    emits += 1;
  });
  // Re-applying the SAME live health + status on every status-bar tick is the steady-state norm.
  runtime.applyStatusBarSnapshot(idleSnapshot, { running: true, health });
  runtime.applyStatusBarSnapshot(idleSnapshot, { running: true, health });
  assert.equal(emits, 0, "no-op re-apply of identical health emits nothing");
});
