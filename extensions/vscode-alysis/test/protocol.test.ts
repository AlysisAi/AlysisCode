import assert from "node:assert/strict";
import test from "node:test";

import {
  BridgeHealthError,
  MANAGEMENT_BRIDGE_METHODS,
  OPTIONAL_BRIDGE_METHODS,
  PROTOCOL_VERSION,
  REQUIRED_BRIDGE_METHODS,
  hasBridgeMethod,
  missingRequiredMethods,
  parseHealthJson,
  protocolRequest
} from "../src/client/AlysisProtocol";

test("parseHealthJson accepts compatible bridge health", () => {
  const health = parseHealthJson(JSON.stringify(healthPayload()));

  assert.equal(health.ok, true);
  assert.equal(health.protocol_version, PROTOCOL_VERSION);
  assert.equal(health.capabilities.transport, "stdio-jsonl");
  assert.deepEqual(missingRequiredMethods(health.capabilities.methods), []);
});

test("parseHealthJson rejects invalid JSON", () => {
  assert.throws(() => parseHealthJson("not json"), BridgeHealthError);
});

test("parseHealthJson rejects unsupported protocol version", () => {
  const payload = healthPayload({ protocol_version: "2" });
  assert.throws(() => parseHealthJson(JSON.stringify(payload)), /Unsupported Alysis Code IDE protocol version/);
});

test("parseHealthJson rejects missing baseline bridge methods", () => {
  const capabilities = healthPayload().capabilities as Record<string, unknown>;
  const payload = healthPayload({
    capabilities: {
      ...capabilities,
      methods: ["initialize", "health"]
    }
  });

  assert.throws(() => parseHealthJson(JSON.stringify(payload)), /missing required IDE methods/);
});

test("parseHealthJson accepts bridge health with only baseline Cockpit methods", () => {
  const withoutOptional = parseHealthJson(JSON.stringify(healthPayload()));
  assert.deepEqual(missingRequiredMethods(withoutOptional.capabilities.methods), []);
  assert.equal(hasBridgeMethod(withoutOptional.capabilities, "chat.send"), false);
  assert.equal(hasBridgeMethod(withoutOptional.capabilities, "code.review.start"), false);
});

test("parseHealthJson treats Chat, review, and management methods as optional feature methods", () => {
  const withOptional = parseHealthJson(
    JSON.stringify(
      healthPayload({
        capabilities: {
          ...(healthPayload().capabilities as Record<string, unknown>),
          methods: [...REQUIRED_BRIDGE_METHODS, ...OPTIONAL_BRIDGE_METHODS]
        }
      })
    )
  );
  assert.equal(hasBridgeMethod(withOptional.capabilities, "chat.queue.list"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "checkpoint.revert"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "permission.evaluate"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "permission.session.revoke"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "code.review.start"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "code.review.result"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "session.tasks.replace"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "session.questions.create"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "session.questions.answer"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "bridge.shutdown"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "browser.start"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "browser.artifact.read"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "browser.close"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "config.get"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "report.create"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "mcp.status"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "mcp.server.status"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "mcp.server.enable"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "mcp.server.disable"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "mcp.server.restart"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "hooks.list"), true);
  assert.equal(hasBridgeMethod(withOptional.capabilities, "ext.search"), true);
  assert.equal(MANAGEMENT_BRIDGE_METHODS.includes("doctor.bundle"), true);
  const advertisedManagementMethods: readonly string[] = MANAGEMENT_BRIDGE_METHODS;
  assert.equal(advertisedManagementMethods.includes("hooks.watch"), false);
  assert.equal(advertisedManagementMethods.includes("mcp.server.status"), false);
});

test("protocolRequest serializes protocol v1 requests", () => {
  assert.deepEqual(protocolRequest("req-1", "health"), {
    protocol_version: "1",
    id: "req-1",
    method: "health",
    params: {}
  });
});

function healthPayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    ok: true,
    name: "alysis-ide-bridge",
    alysis_version: "0.1.4",
    protocol_version: "1",
    capabilities: {
      protocol_version: "1",
      methods: [...REQUIRED_BRIDGE_METHODS],
      events: ["message_delta", "tool_call_started"],
      modes: ["readonly", "review", "auto"],
      transport: "stdio-jsonl",
      features: {
        structured_surface_events: true
      }
    },
    ...overrides
  };
}
