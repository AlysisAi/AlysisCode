import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

import {
  CLI_INSTALL_COMMAND,
  CLI_UPGRADE_COMMAND,
  MANAGED_BROWSER_DIRECT_LOOPBACK_SECURITY_FLAGS,
  MANAGED_BROWSER_METHODS,
  MANAGED_BROWSER_OWNER_METHODS,
  MANAGED_BROWSER_SECURITY_FLAGS,
  MIN_RECOMMENDED_ALYSIS_CLI_VERSION,
  PERSONAS_METHODS,
  cliHealthStatusForCompatibilityError,
  evaluateBridgeCompatibility,
  evaluateCliVersion,
  featureDisabledReason
} from "../src/client/compatibility";
import {
  BridgeHealthError,
  PROTOCOL_VERSION,
  REQUIRED_BRIDGE_METHODS,
  protocolMismatchDirection
} from "../src/client/AlysisProtocol";

test("compatibility contract allows baseline Cockpit while disabling missing feature methods", () => {
  const compatibility = evaluateBridgeCompatibility(healthPayload([...REQUIRED_BRIDGE_METHODS]));

  assert.equal(compatibility.features.baselineCockpit.supported, true);
  assert.equal(compatibility.features.chatRun.supported, false);
  assert.match(featureDisabledReason(compatibility, "chatRun"), /chat\.send/);
});

test("compatibility contract supports chat while disabling management features independently", () => {
  const compatibility = evaluateBridgeCompatibility(
    healthPayload([
      ...REQUIRED_BRIDGE_METHODS,
      "session.create",
      "chat.send",
      "run.start",
      "session.cancel",
      "approval.respond",
      "job.status",
      "session.list",
      "session.getEvents",
      "artifact.list",
      "artifact.read"
    ])
  );

  assert.equal(compatibility.features.chatRun.supported, true);
  assert.equal(compatibility.features.doctor.supported, false);
  assert.match(featureDisabledReason(compatibility, "doctor"), /Needs a newer Alysis Code CLI/);
});

test("managed browser compatibility is fail-closed until the complete method set is advertised", () => {
  const missing = evaluateBridgeCompatibility(healthPayload([...REQUIRED_BRIDGE_METHODS]));
  assert.equal(missing.features.managedBrowser.supported, false);
  assert.deepEqual(
    missing.features.managedBrowser.missingMethods,
    [
      ...MANAGED_BROWSER_METHODS,
      ...MANAGED_BROWSER_OWNER_METHODS,
      ...MANAGED_BROWSER_SECURITY_FLAGS.map((flag) => `features.managed_browser.${flag}`)
    ]
  );

  const partial = evaluateBridgeCompatibility(
    healthPayload(
      [...REQUIRED_BRIDGE_METHODS, ...MANAGED_BROWSER_OWNER_METHODS, "browser.start", "browser.status"],
      ["readonly", "review"],
      managedBrowserSecurityFeatures()
    )
  );
  assert.equal(partial.features.managedBrowser.supported, false);
  assert.match(featureDisabledReason(partial, "managedBrowser"), /browser\.navigate/);

  const complete = evaluateBridgeCompatibility(
    healthPayload(
      [...REQUIRED_BRIDGE_METHODS, ...MANAGED_BROWSER_OWNER_METHODS, ...MANAGED_BROWSER_METHODS],
      ["readonly", "review"],
      managedBrowserSecurityFeatures()
    )
  );
  assert.equal(complete.features.managedBrowser.supported, true);
  assert.equal(complete.features.managedBrowser.reason, null);
});

test("managed browser compatibility rejects every absent or false security attestation", () => {
  const methods = [
    ...REQUIRED_BRIDGE_METHODS,
    ...MANAGED_BROWSER_OWNER_METHODS,
    ...MANAGED_BROWSER_METHODS
  ];
  for (const flag of MANAGED_BROWSER_SECURITY_FLAGS) {
    for (const value of [undefined, false] as const) {
      const capability = Object.fromEntries(
        MANAGED_BROWSER_SECURITY_FLAGS
          .filter((candidate) => candidate !== flag || value !== undefined)
          .map((candidate) => [candidate, candidate === flag ? value : true])
      );
      const compatibility = evaluateBridgeCompatibility(
        healthPayload(methods, ["readonly", "review"], {
          managed_browser: capability
        })
      );
      assert.equal(
        compatibility.features.managedBrowser.supported,
        false,
        `${flag}=${String(value)}`
      );
      assert.ok(
        compatibility.features.managedBrowser.missingMethods.includes(
          `features.managed_browser.${flag}`
        ),
        flag
      );
      assert.equal(compatibility.features.managedBrowser.reason?.category, "security_model");
    }
  }
});

test("direct IDE loopback browsing has an independent fail-closed actor-isolation gate", () => {
  const methods = [
    ...REQUIRED_BRIDGE_METHODS,
    ...MANAGED_BROWSER_OWNER_METHODS,
    ...MANAGED_BROWSER_METHODS
  ];
  const publicOnly = evaluateBridgeCompatibility(
    healthPayload(methods, ["readonly", "review"], managedBrowserSecurityFeatures())
  );
  assert.equal(publicOnly.features.managedBrowser.supported, true);
  assert.equal(publicOnly.features.managedBrowserDirectLoopback.supported, false);
  assert.deepEqual(
    publicOnly.features.managedBrowserDirectLoopback.missingMethods,
    MANAGED_BROWSER_DIRECT_LOOPBACK_SECURITY_FLAGS.map(
      (flag) => `features.managed_browser.${flag}`
    )
  );

  const complete = evaluateBridgeCompatibility(
    healthPayload(methods, ["readonly", "review"], {
      managed_browser: {
        ...(managedBrowserSecurityFeatures().managed_browser as Record<string, unknown>),
        ...Object.fromEntries(
          MANAGED_BROWSER_DIRECT_LOOPBACK_SECURITY_FLAGS.map((flag) => [flag, true])
        )
      }
    })
  );
  assert.equal(complete.features.managedBrowser.supported, true);
  assert.equal(complete.features.managedBrowserDirectLoopback.supported, true);
  assert.equal(complete.features.managedBrowserDirectLoopback.reason, null);
});

test("FE-8: per-domain management gates are independent (config on does not enable doctor)", () => {
  const withConfig = evaluateBridgeCompatibility(
    healthPayload([...REQUIRED_BRIDGE_METHODS, "config.get", "config.set", "config.schema", "config.validate"])
  );
  // config.* present -> manageConfig enabled; doctor.* absent -> doctor still gated (no monolith).
  assert.equal(withConfig.features.manageConfig.supported, true);
  assert.equal(withConfig.features.doctor.supported, false);
  assert.match(featureDisabledReason(withConfig, "doctor"), /doctor\./);

  const withDoctor = evaluateBridgeCompatibility(
    healthPayload([...REQUIRED_BRIDGE_METHODS, "doctor.summary", "doctor.providers", "doctor.bundle"])
  );
  assert.equal(withDoctor.features.doctor.supported, true);
  assert.equal(withDoctor.features.manageConfig.supported, false);
});

test("compatibility setup commands are safe static commands", () => {
  assert.equal(CLI_INSTALL_COMMAND, "pipx install alysis-code");
  assert.equal(CLI_UPGRADE_COMMAND, "pipx upgrade alysis-code");
  assert.doesNotMatch(CLI_INSTALL_COMMAND + CLI_UPGRADE_COMMAND, /key|token|secret|sk-/i);
});

test("Phase 3 reserved feature gates default OFF with today's bridge", () => {
  const compatibility = evaluateBridgeCompatibility(healthPayload([...REQUIRED_BRIDGE_METHODS]));
  const reserved = ["providersProfiles", "modesFullaccess"] as const;
  for (const id of reserved) {
    assert.equal(compatibility.features[id].supported, false, `${id} should default OFF`);
  }
});

test("FE-8: reserved ids resolve to the REAL bridge methods and light up when advertised", () => {
  const compatibility = evaluateBridgeCompatibility(
    healthPayload([
      ...REQUIRED_BRIDGE_METHODS,
      "config.get",
      "config.set",
      "profile.list",
      "profile.use"
    ])
  );
  assert.equal(compatibility.features.providersProfiles.supported, true, "config.* + profile.*");
  // providersProfiles needs ALL of config.* + profile.* — not just profile.list (the pre-FE-8 bug).
  assert.equal(
    evaluateBridgeCompatibility(healthPayload([...REQUIRED_BRIDGE_METHODS, "profile.list"])).features.providersProfiles.supported,
    false
  );
});

test("structured compatibility reasons classify security-model blocks, version gates, and protocol gaps", () => {
  const blocked = evaluateBridgeCompatibility(
    healthPayload([...REQUIRED_BRIDGE_METHODS], ["readonly", "review", "auto", "fullaccess"], {
      unsafe_modes: {
        blocked_until_security_model: ["auto", "fullaccess"],
        reason: "IDE v1 lacks strong enough cancellation, sandbox, and approval guarantees."
      }
    })
  );
  assert.equal(blocked.features.modesFullaccess.supported, false);
  assert.equal(blocked.features.modesFullaccess.reason?.category, "security_model");
  assert.equal(blocked.features.modesFullaccess.reason?.message, "Blocked pending the security model");
  assert.match(blocked.features.modesFullaccess.reason?.detail ?? "", /cancellation/);

  const missingManagement = evaluateBridgeCompatibility(healthPayload([...REQUIRED_BRIDGE_METHODS]));
  assert.equal(missingManagement.features.tools.reason?.category, "version_gate");
  assert.match(missingManagement.features.tools.reason?.message ?? "", /Needs a newer Alysis Code CLI/);
  assert.match(missingManagement.features.tools.reason?.message ?? "", /tools\.catalog/);

  const incompatible = evaluateBridgeCompatibility({
    ...healthPayload([...REQUIRED_BRIDGE_METHODS]),
    protocol_version: "0.0.0",
    capabilities: {
      ...healthPayload([...REQUIRED_BRIDGE_METHODS]).capabilities,
      protocol_version: "0.0.0"
    }
  });
  assert.equal(incompatible.protocol.reason?.category, "version_gate");
  assert.match(featureDisabledReason(incompatible, "tools"), /Needs a newer Alysis Code CLI/);
});

test("modesFullaccess reflects the bridge capabilities.modes list, not a method", () => {
  assert.equal(
    evaluateBridgeCompatibility(healthPayload([...REQUIRED_BRIDGE_METHODS])).features.modesFullaccess.supported,
    false
  );
  assert.equal(
    evaluateBridgeCompatibility(
      healthPayload([...REQUIRED_BRIDGE_METHODS], ["readonly", "review", "auto", "fullaccess"])
    ).features.modesFullaccess.supported,
    true
  );
});

test("FE-12: providersProfiles and manageSessions gate independently on their own methods", () => {
  // The provider/profile switcher gate needs config.get + config.set + profile.list + profile.use.
  const withProviders = evaluateBridgeCompatibility(
    healthPayload([...REQUIRED_BRIDGE_METHODS, "config.get", "config.set", "profile.list", "profile.use"])
  );
  assert.equal(withProviders.features.providersProfiles.supported, true, "providers gate lights up on its methods");
  assert.equal(withProviders.features.manageSessions.supported, false, "sessions gate stays off independently");

  // The sessions detail/usage gate needs session.show + session.usage + session.score.
  const withSessions = evaluateBridgeCompatibility(
    healthPayload([...REQUIRED_BRIDGE_METHODS, "session.show", "session.usage", "session.score"])
  );
  assert.equal(withSessions.features.manageSessions.supported, true, "sessions gate lights up on its methods");
  assert.equal(withSessions.features.providersProfiles.supported, false, "providers gate stays off independently");

  // Baseline: both default OFF.
  const baseline = evaluateBridgeCompatibility(healthPayload([...REQUIRED_BRIDGE_METHODS]));
  assert.equal(baseline.features.providersProfiles.supported, false);
  assert.equal(baseline.features.manageSessions.supported, false);
});

test("FE-10: cliHealthStatusForCompatibilityError agrees with the detection taxonomy", () => {
  // A transport failure on the live-operation path must NOT be mislabeled a version gap — it is the
  // same "can't reach the CLI" cause the detection path renders as a Locate-CLI card.
  assert.equal(cliHealthStatusForCompatibilityError(new BridgeHealthError("unsupported_transport", "bad transport")), "unreachable");
  // Genuine version/method gaps stay "incompatible" (Upgrade).
  for (const code of ["unsupported_protocol_version", "incompatible_bridge", "ide_bridge_missing"]) {
    assert.equal(cliHealthStatusForCompatibilityError(new BridgeHealthError(code, code)), "incompatible", code);
  }
  // Launched-but-unhealthy stays "broken"; a not-found binary (ENOENT) is "unreachable".
  assert.equal(cliHealthStatusForCompatibilityError(new BridgeHealthError("bridge_unhealthy", "bad")), "broken");
  const enoent = Object.assign(new Error("spawn ENOENT"), { code: "ENOENT" });
  assert.equal(cliHealthStatusForCompatibilityError(enoent), "unreachable");
});

test("a CLI newer than the extension is told to update the extension, never to upgrade the CLI", () => {
  const newerCli = { ...healthPayload([...REQUIRED_BRIDGE_METHODS]), protocol_version: "99" };
  const compatibility = evaluateBridgeCompatibility(newerCli);

  assert.equal(compatibility.protocol.compatible, false);
  assert.equal(compatibility.protocol.direction, "extension_outdated");
  assert.match(compatibility.protocol.message ?? "", /update the Alysis Code VS Code extension/i);
  assert.doesNotMatch(compatibility.protocol.message ?? "", /upgrade the alysis cli/i);

  const olderCli = { ...healthPayload([...REQUIRED_BRIDGE_METHODS]), protocol_version: "0" };
  const outdated = evaluateBridgeCompatibility(olderCli);
  assert.equal(outdated.protocol.direction, "cli_outdated");
  assert.match(outdated.protocol.message ?? "", /upgrade the Alysis Code CLI/i);

  assert.equal(protocolMismatchDirection(PROTOCOL_VERSION, PROTOCOL_VERSION), "unknown");
  assert.equal(protocolMismatchDirection("not-a-version"), "unknown");
});

test("the minimum recommended CLI version is a real comparison, not a decorative constant", () => {
  const current = evaluateBridgeCompatibility(healthPayload([...REQUIRED_BRIDGE_METHODS]));
  assert.equal(current.cliVersion?.satisfied, true);
  assert.equal(current.cliVersion?.current, MIN_RECOMMENDED_ALYSIS_CLI_VERSION);
  assert.equal(current.cliVersion?.message, null);

  const older = evaluateCliVersion("0.1.3", "0.1.4");
  assert.equal(older.satisfied, false);
  assert.match(older.message ?? "", /0\.1\.3 is older than the 0\.1\.4/);
  assert.match(older.message ?? "", new RegExp(CLI_UPGRADE_COMMAND.replace(/ /g, "\\s")));

  assert.equal(evaluateCliVersion("0.2.0", "0.1.4").satisfied, true);
  assert.equal(evaluateCliVersion("1.0", "0.1.4").satisfied, true);
  assert.equal(evaluateCliVersion("0.1.4-rc1", "0.1.4").satisfied, true, "a release candidate of the minimum is not older");
  // An unreadable or absent version is never treated as evidence that the CLI is too old.
  assert.equal(evaluateCliVersion("unknown", "0.1.4").satisfied, true);
  assert.equal(evaluateCliVersion("", "0.1.4").satisfied, true);
});

test("the minimum recommended CLI version is the CLI this monorepo ships and the docs agree", () => {
  // The constant means "tested against". The CLI under test is the one in this tree, so a stale
  // constant silently advertises a version nobody has run this extension with.
  const packageRoot = resolve(__dirname, "../..");
  const repoRoot = resolve(packageRoot, "../..");
  const pyproject = readFileSync(resolve(repoRoot, "pyproject.toml"), "utf8");
  const cliVersion = /^version\s*=\s*"([^"]+)"/m.exec(pyproject)?.[1];
  assert.ok(cliVersion, "pyproject.toml must declare [project].version");
  assert.equal(MIN_RECOMMENDED_ALYSIS_CLI_VERSION, cliVersion);

  const architecture = readFileSync(resolve(packageRoot, "docs/architecture.md"), "utf8");
  assert.match(
    architecture,
    new RegExp(`\`alysis-code\` \`${MIN_RECOMMENDED_ALYSIS_CLI_VERSION.replace(/\./g, "\\.")}\``),
    "docs/architecture.md must state the same minimum recommended CLI version"
  );
});

test("personas light up only when BOTH persona methods are advertised", () => {
  const complete = evaluateBridgeCompatibility(
    healthPayload([...REQUIRED_BRIDGE_METHODS, ...PERSONAS_METHODS])
  );
  assert.equal(complete.features.personas.supported, true);
  assert.deepEqual(complete.features.personas.missingMethods, []);

  // Half a pair is a control that cannot keep its promise, so it stays gated.
  for (const half of PERSONAS_METHODS) {
    const partial = evaluateBridgeCompatibility(healthPayload([...REQUIRED_BRIDGE_METHODS, half]));
    assert.equal(partial.features.personas.supported, false, half);
    assert.equal(partial.features.personas.reason?.category, "version_gate");
  }

  const missing = evaluateBridgeCompatibility(healthPayload([...REQUIRED_BRIDGE_METHODS]));
  assert.equal(missing.features.personas.supported, false);
  assert.deepEqual(missing.features.personas.missingMethods, [...PERSONAS_METHODS]);
  assert.match(featureDisabledReason(missing, "personas"), /Needs a newer Alysis Code CLI/);
  assert.match(featureDisabledReason(missing, "personas"), /session\.personas\.list/);
});

function healthPayload(
  methods: string[],
  modes: string[] = ["readonly", "review"],
  features: Record<string, unknown> = {}
): any {
  return {
    ok: true,
    name: "alysis-ide-bridge",
    alysis_version: MIN_RECOMMENDED_ALYSIS_CLI_VERSION,
    protocol_version: PROTOCOL_VERSION,
    capabilities: {
      protocol_version: PROTOCOL_VERSION,
      methods,
      events: [],
      modes,
      transport: "stdio-jsonl",
      features
    }
  };
}

function managedBrowserSecurityFeatures(): Record<string, unknown> {
  return {
    managed_browser: Object.fromEntries(
      MANAGED_BROWSER_SECURITY_FLAGS.map((flag) => [flag, true])
    )
  };
}
