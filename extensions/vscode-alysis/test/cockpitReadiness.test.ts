import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

import {
  ASSETS_METHODS,
  BASELINE_COCKPIT_METHODS,
  CHAT_RUN_METHODS,
  FORGE_PLAN_SYNC_METHODS,
  FORGE_SWARM_METHODS,
  MANAGE_CONFIG_METHODS,
  MANAGE_PROFILES_METHODS,
  DOCTOR_METHODS,
  emptyCompatibilitySnapshot,
  evaluateBridgeCompatibility
} from "../src/client/compatibility";
import {
  CockpitBlocker,
  CockpitReadinessInput,
  buildCockpitReadiness,
  dedupeAndCapBlockers
} from "../src/chat/CockpitReadiness";
import type { CockpitRuntimeSnapshot } from "../src/chat/CockpitRuntimeState";

const CONTRIBUTED_COMMANDS = contributedCommandIds();
// The only non-Alysis Code command a blocker may offer: VS Code's built-in Workspace Trust editor.
const BUILT_IN_COMMANDS = new Set(["workbench.trust.manage"]);

test("a missing CLI produces ONE root-cause blocker, not one per symptom", () => {
  const readiness = buildCockpitReadiness(
    input({
      runtime: runtimeSnapshot({
        cliHealth: { status: "unreachable", message: "Alysis Code CLI could not be launched." },
        bridgeProcess: { status: "error", message: "bridge health failed" },
        bridgeProtocol: { status: "missing_methods", message: "artifact.read missing" }
      })
    })
  );

  assert.deepEqual(readiness.blockers.map((blocker) => blocker.id), ["runtime_missing"]);
  assert.equal(readiness.blockers[0].severity, "error");
  assert.equal(readiness.blockers[0].title, "Locate the CLI on this computer");
  assert.deepEqual(
    readiness.blockers[0].actions.map((action) => action.command),
    ["alysis.locateCli", "alysis.copyCliInstallCommand", "alysis.openSetupGuide"]
  );
});

test("the composer stays locked while the engine is down even when the provider is ready", () => {
  const readiness = buildCockpitReadiness(
    input({
      runtime: runtimeSnapshot({ cliHealth: { status: "unreachable", message: null } }),
      provider: { status: "ready", reason: "The active provider, model, and reasoning settings are ready." }
    })
  );

  assert.equal(readiness.ok, false);
});

test("a checked-out, healthy, provider-ready cockpit reports ok with no blockers", () => {
  const readiness = buildCockpitReadiness(input({}));

  assert.equal(readiness.ok, true);
  assert.deepEqual(readiness.blockers, []);
});

test("an unchecked engine does not block the first task (the bridge starts lazily)", () => {
  const readiness = buildCockpitReadiness(
    input({ runtime: runtimeSnapshot({ cliHealth: { status: "unknown", message: null } }) })
  );

  assert.equal(readiness.ok, true);
  assert.deepEqual(readiness.blockers, []);
});

test("a managed runtime is never told to install a separate CLI", () => {
  const readiness = buildCockpitReadiness(
    input({
      runtime: runtimeSnapshot({
        runtimeSelection: { origin: "managed", production: true, message: null },
        cliHealth: { status: "unreachable", message: null }
      })
    })
  );

  const commands = readiness.blockers[0].actions.map((action) => action.command);
  assert.equal(commands.includes("alysis.copyCliInstallCommand"), false);
  assert.equal(commands.includes("alysis.locateCli"), false);
  assert.deepEqual(commands, ["alysis.showBridgeHealth", "alysis.openSetupGuide"]);
});

test("initial runtime validation stays locked but shows progress instead of a broken install", () => {
  const readiness = buildCockpitReadiness(input({
    cliTrusted: false,
    runtime: runtimeSnapshot({
      runtimeSelection: { origin: "unavailable", production: false, message: "validating", validating: true },
      cliOrigin: { status: "blocked", message: "validating" }
    })
  }));
  assert.equal(readiness.ok, false);
  assert.equal(readiness.blockers[0].id, "runtime_validating");
  assert.equal(readiness.blockers[0].progress, true);
  assert.deepEqual(readiness.blockers[0].actions, []);
});

test("an unavailable managed runtime is its own root cause and offers repair, not install", () => {
  const readiness = buildCockpitReadiness(
    input({
      runtime: runtimeSnapshot({
        runtimeSelection: { origin: "unavailable", production: true, message: "bundle missing" },
        cliHealth: { status: "unreachable", message: null }
      })
    })
  );

  assert.deepEqual(readiness.blockers.map((blocker) => blocker.id), ["runtime_missing"]);
  assert.equal(readiness.blockers[0].title, "The Alysis Code engine is unavailable");
  assert.equal(readiness.ok, false);
});

test("an extension-outdated protocol mismatch says update the extension, never upgrade the CLI", () => {
  const compatibility = emptyCompatibilitySnapshot();
  compatibility.protocol.direction = "extension_outdated";
  const readiness = buildCockpitReadiness(
    input({
      runtime: runtimeSnapshot({
        cliHealth: { status: "extension_outdated", message: null },
        compatibility
      })
    })
  );

  assert.deepEqual(readiness.blockers.map((blocker) => blocker.id), ["extension_outdated"]);
  assert.equal(readiness.blockers[0].actions[0].command, "alysis.checkForUpdates");
  assert.equal(readiness.blockers[0].actions[0].label, "Update extension");
  assert.equal(
    readiness.blockers[0].actions.some((action) => action.command === "alysis.copyCliUpgradeCommand"),
    false
  );
});

test("an outdated CLI blocks and offers the upgrade command", () => {
  const readiness = buildCockpitReadiness(
    input({ runtime: runtimeSnapshot({ cliHealth: { status: "incompatible", message: null } }) })
  );

  assert.deepEqual(readiness.blockers.map((blocker) => blocker.id), ["cli_outdated"]);
  assert.equal(readiness.blockers[0].severity, "error");
  assert.equal(readiness.blockers[0].actions[0].command, "alysis.copyCliUpgradeCommand");
  assert.equal(readiness.ok, false);
});

test("permanently gated capabilities never turn an up-to-date engine into 'An update is needed'", () => {
  // A current CLI advertises every method the extension can use; the only unsupported features are
  // the ones that have no backend at all (Open as PR, Keep/Discard) or are security-model gated.
  const compatibility = evaluateBridgeCompatibility({
    ok: true,
    name: "alysis",
    alysis_version: "0.14.1",
    protocol_version: "1",
    capabilities: {
      protocol_version: "1",
      methods: [
        ...BASELINE_COCKPIT_METHODS,
        ...CHAT_RUN_METHODS,
        ...FORGE_PLAN_SYNC_METHODS,
        ...FORGE_SWARM_METHODS,
        ...MANAGE_CONFIG_METHODS,
        ...MANAGE_PROFILES_METHODS,
        ...DOCTOR_METHODS,
        ...ASSETS_METHODS
      ],
      events: [],
      modes: ["readonly", "review", "auto"],
      transport: "stdio"
    }
  });
  assert.equal(compatibility.features.forgePr.supported, false);
  assert.equal(compatibility.features.diffApplyDiscard.supported, false);

  const readiness = buildCockpitReadiness(input({ runtime: runtimeSnapshot({ compatibility }) }));

  // Some version-gated features ARE missing here (doctor bundle etc. are only partially listed), so
  // the blocker may exist — but it must be driven only by version_gate reasons, never by the
  // permanently gated slots. Count exactly what the card claims.
  const outdated = readiness.blockers.find((blocker) => blocker.id === "cli_outdated");
  const versionGated = Object.values(compatibility.features).filter(
    (feature) => !feature.supported && feature.reason?.category === "version_gate"
  ).length;
  if (versionGated === 0) {
    assert.equal(outdated, undefined);
  } else {
    assert.ok(outdated);
    assert.ok(outdated.detail.startsWith(`${versionGated} `), outdated.detail);
  }
});

test("a fully capable engine reports no upgrade blocker at all", () => {
  const compatibility = emptyCompatibilitySnapshot();
  compatibility.protocol.current = "1";
  compatibility.protocol.compatible = true;
  for (const feature of Object.values(compatibility.features)) {
    feature.supported = true;
    feature.reason = null;
    feature.disabledReason = null;
    feature.missingMethods = [];
  }
  compatibility.features.forgePr.supported = false;
  compatibility.features.forgePr.reason = { category: "not_applicable", message: "Not available in the IDE" };
  compatibility.features.diffApplyDiscard.supported = false;
  compatibility.features.diffApplyDiscard.reason = { category: "not_applicable", message: "Not available in the IDE" };
  compatibility.features.modesFullaccess.supported = false;
  compatibility.features.modesFullaccess.reason = { category: "security_model", message: "Blocked pending the security model" };

  const readiness = buildCockpitReadiness(input({ runtime: runtimeSnapshot({ compatibility }) }));

  assert.deepEqual(readiness.blockers, []);
  assert.equal(readiness.ok, true);
});

test("a still-loading provider check is neutral progress, not a warning with actions", () => {
  const readiness = buildCockpitReadiness(input({ provider: { status: "loading", reason: "Checking..." } }));

  assert.equal(readiness.blockers[0].progress, true);
  assert.deepEqual(readiness.blockers[0].actions, []);
});

test("an untrusted folder limits without locking, and blocks outright when it blocks execution", () => {
  const limited = buildCockpitReadiness(input({ workspaceTrusted: false }));
  assert.deepEqual(limited.blockers.map((blocker) => blocker.id), ["workspace_untrusted"]);
  assert.equal(limited.blockers[0].severity, "warning");
  assert.equal(limited.blockers[0].title, "Limited until you trust this folder");
  assert.equal(limited.blockers[0].actions[0].command, "workbench.trust.manage");
  assert.equal(limited.ok, true);

  const blocked = buildCockpitReadiness(input({ workspaceTrusted: false, cliTrusted: false }));
  // One id, one card: the trust warning must not double-render beside the trust error.
  assert.deepEqual(blocked.blockers.map((blocker) => blocker.id), ["workspace_untrusted"]);
  assert.equal(blocked.blockers[0].severity, "error");
  assert.equal(blocked.ok, false);
});

test("a blocked executable origin in a trusted folder points at Locate CLI", () => {
  const blocked = buildCockpitReadiness(input({ cliTrusted: false }));

  assert.deepEqual(blocked.blockers.map((blocker) => blocker.id), ["cli_blocked"]);
  assert.equal(blocked.blockers[0].actions[0].command, "alysis.locateCli");
  assert.equal(blocked.ok, false);
});

test("provider selection gates the composer in both incomplete and loading states", () => {
  const incomplete = buildCockpitReadiness(
    input({ provider: { status: "incomplete", reason: "Choose a model for the active provider before sending a task." } })
  );
  assert.deepEqual(incomplete.blockers.map((blocker) => blocker.id), ["provider_incomplete"]);
  assert.equal(incomplete.blockers[0].detail, "Choose a model for the active provider before sending a task.");
  assert.deepEqual(
    incomplete.blockers[0].actions.map((action) => action.command),
    ["alysis.configureProvider", "alysis.showModels"]
  );
  assert.equal(incomplete.ok, false);

  const loading = buildCockpitReadiness(input({ provider: { status: "loading", reason: "Checking..." } }));
  assert.deepEqual(loading.blockers.map((blocker) => blocker.id), ["provider_loading"]);
  assert.equal(loading.blockers[0].severity, "warning");
  // Fail closed: "still checking" is never a green light.
  assert.equal(loading.ok, false);
});

test("blockers are deduped by id, severity sorted, and capped at three", () => {
  const capped = dedupeAndCapBlockers([
    blocker("a", "warning"),
    blocker("b", "error"),
    blocker("a", "error"),
    blocker("c", "warning"),
    blocker("d", "error")
  ]);

  assert.deepEqual(capped.map((entry) => entry.id), ["b", "d", "a"]);
  assert.equal(capped.length, 3);
});

test("every blocker action names a real contributed command and stays imperative and short", () => {
  const runtimes: CockpitRuntimeSnapshot[] = [
    runtimeSnapshot({ cliHealth: { status: "unreachable", message: null } }),
    runtimeSnapshot({ cliHealth: { status: "broken", message: null } }),
    runtimeSnapshot({ cliHealth: { status: "incompatible", message: null } }),
    runtimeSnapshot({ cliHealth: { status: "extension_outdated", message: null } }),
    runtimeSnapshot({ bridgeProcess: { status: "error", message: null } }),
    runtimeSnapshot({ sandboxDoctor: { status: "failed", message: null } }),
    runtimeSnapshot({ runtimeSelection: { origin: "unavailable", production: true, message: null } })
  ];
  const readinessStates = [
    ...runtimes.map((runtime) => buildCockpitReadiness(input({ runtime }))),
    buildCockpitReadiness(input({ workspaceTrusted: false })),
    buildCockpitReadiness(input({ cliTrusted: false })),
    buildCockpitReadiness(input({ provider: { status: "incomplete", reason: "Choose a provider." } }))
  ];

  for (const readiness of readinessStates) {
    assert.ok(readiness.blockers.length <= 3);
    for (const entry of readiness.blockers) {
      assert.ok(entry.title.length <= 60, `title too long: ${entry.title}`);
      assert.doesNotMatch(entry.title, /\(|\b\d{3}\b|_/, `title carries a code: ${entry.title}`);
      assert.doesNotMatch(entry.detail, /\(|\bhttp\b|Error:/i, `detail carries a code: ${entry.detail}`);
      assert.ok(entry.detail.length > 0);
      assert.ok(
        entry.actions.filter((action) => action.primary).length <= 1,
        `more than one primary action on ${entry.id}`
      );
      for (const action of entry.actions) {
        assert.ok(action.label.length <= 24, `label too long: ${action.label}`);
        assert.ok(
          CONTRIBUTED_COMMANDS.has(action.command) || BUILT_IN_COMMANDS.has(action.command),
          `unknown command: ${action.command}`
        );
      }
    }
  }
});

function input(overrides: Partial<CockpitReadinessInput>): CockpitReadinessInput {
  return {
    runtime: runtimeSnapshot({}),
    workspaceTrusted: true,
    cliTrusted: true,
    provider: { status: "ready", reason: "ready" },
    ...overrides
  };
}

function runtimeSnapshot(overrides: Partial<CockpitRuntimeSnapshot>): CockpitRuntimeSnapshot {
  return {
    runtimeSelection: { origin: "path", production: false, message: null },
    cliOrigin: { status: "trusted", message: null },
    cliHealth: { status: "ok", message: null },
    bridgeProcess: { status: "ready", message: null },
    bridgeProtocol: { status: "ok", message: null },
    sandboxDoctor: { status: "unknown", message: null },
    compatibility: emptyCompatibilitySnapshot(),
    events: [],
    ...overrides
  } as CockpitRuntimeSnapshot;
}

function blocker(id: string, severity: CockpitBlocker["severity"]): CockpitBlocker {
  return { id, severity, title: id, detail: id, actions: [] };
}

function contributedCommandIds(): Set<string> {
  const manifest = JSON.parse(
    readFileSync(resolve(__dirname, "../..", "package.json"), "utf8")
  ) as { contributes?: { commands?: Array<{ command?: string }> } };
  return new Set(
    (manifest.contributes?.commands ?? [])
      .map((entry) => entry.command)
      .filter((command): command is string => typeof command === "string")
  );
}
