import assert from "node:assert/strict";
import test from "node:test";

import { evaluateWorkspaceTrust } from "../src/security/workspaceTrust";

test("workspace trust allows safe setup and readonly placeholders in untrusted workspaces", () => {
  assert.equal(evaluateWorkspaceTrust(false, "bridgeHealth").allowed, true);
  assert.equal(evaluateWorkspaceTrust(false, "configureProvider").allowed, true);
  assert.equal(evaluateWorkspaceTrust(false, "runDoctor").allowed, true);
  assert.equal(evaluateWorkspaceTrust(false, "readonlyPlaceholder").allowed, true);
});

test("workspace trust blocks mutating actions in untrusted workspaces", () => {
  assert.equal(evaluateWorkspaceTrust(false, "mutatingAction").allowed, false);
});

test("workspace trust allows all scaffold actions in trusted workspaces", () => {
  assert.equal(evaluateWorkspaceTrust(true, "mutatingAction").allowed, true);
});
