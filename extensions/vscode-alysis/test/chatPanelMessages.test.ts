import assert from "node:assert/strict";
import test from "node:test";

import { isChatPanelMessage } from "../src/chat/webviewMessages";

test("isChatPanelMessage accepts expected webview messages", () => {
  assert.equal(isChatPanelMessage({ type: "submit", text: "hello", requestId: "request-1" }), true);
  assert.equal(isChatPanelMessage({ type: "cancel" }), true);
  assert.equal(isChatPanelMessage({ type: "slash.quickAction", command: "/doctor", requestId: "request-2" }), true);
  assert.equal(isChatPanelMessage({ type: "approval", approvalId: "a1", decision: "deny" }), true);
  assert.equal(isChatPanelMessage({ type: "artifact.refresh" }), true);
  assert.equal(isChatPanelMessage({ type: "artifact.open", artifactId: "session:a.txt" }), true);
  assert.equal(isChatPanelMessage({ type: "command.openSetupGuide" }), true);
  assert.equal(isChatPanelMessage({ type: "command.openOutput" }), true);
  assert.equal(isChatPanelMessage({ type: "command.setCliPath" }), true);
  assert.equal(isChatPanelMessage({ type: "cockpit.retryStatus" }), true);
  assert.equal(isChatPanelMessage({ type: "bridge.restart" }), true);
  assert.equal(isChatPanelMessage({ type: "mode.set", mode: "readonly" }), true);
  assert.equal(isChatPanelMessage({ type: "mode.set", mode: "auto" }), true);
  assert.equal(isChatPanelMessage({ type: "mode.set", mode: "fullaccess" }), true);
  assert.equal(isChatPanelMessage({ type: "session.model.set", model: "gpt-5" }), true);
  assert.equal(isChatPanelMessage({ type: "persona.set", name: "architect" }), true);
  assert.equal(isChatPanelMessage({ type: "persona.set", name: "My.Custom_persona-2" }), true);
  assert.equal(isChatPanelMessage({ type: "profile.use", name: "openai" }), true);
  assert.equal(isChatPanelMessage({ type: "browser.start" }), true);
  assert.equal(isChatPanelMessage({ type: "browser.startLocal" }), true);
  assert.equal(isChatPanelMessage({ type: "browser.navigate", url: "https://example.com" }), true);
  assert.equal(isChatPanelMessage({ type: "browser.snapshot", kind: "accessibility" }), true);
  assert.equal(isChatPanelMessage({ type: "browser.screenshot", fullPage: true }), true);
  assert.equal(isChatPanelMessage({ type: "browser.type", selector: "#field", text: "hello", replace: true }), true);
  assert.equal(isChatPanelMessage({ type: "browser.screenshot.save" }), true);
});

test("isChatPanelMessage rejects unexpected webview messages", () => {
  assert.equal(isChatPanelMessage({ type: "unknown" }), false);
  // Retired plan-and-execute messages are no longer part of the webview contract.
  assert.equal(isChatPanelMessage({ type: "forge.executePreview" }), false);
  assert.equal(isChatPanelMessage({ type: "swarm.start", parallel: 2 }), false);
  assert.equal(isChatPanelMessage({ type: "composerMode.set", mode: "chat" }), false);
  assert.equal(isChatPanelMessage(null), false);
  assert.equal(isChatPanelMessage("submit"), false);
  assert.equal(isChatPanelMessage({ type: "submit", text: "" }), false);
  assert.equal(isChatPanelMessage({ type: "submit" }), false);
  assert.equal(isChatPanelMessage({ type: "submit", text: "hello" }), false);
  assert.equal(isChatPanelMessage({ type: "approval", approvalId: "a1", decision: "approve" }), false);
  assert.equal(isChatPanelMessage({ type: "approval", decision: "deny" }), false);
  assert.equal(isChatPanelMessage({ type: "artifact.open" }), false);
  assert.equal(isChatPanelMessage({ type: "slash.quickAction", command: "doctor", requestId: "r" }), false);
  assert.equal(isChatPanelMessage({ type: "slash.quickAction", command: "/doctor" }), false);
  assert.equal(isChatPanelMessage({ type: "mode.set", mode: "turbo" }), false);
  assert.equal(isChatPanelMessage({ type: "mode.set" }), false);
  assert.equal(isChatPanelMessage({ type: "session.model.set" }), false);
  assert.equal(isChatPanelMessage({ type: "session.model.set", model: "" }), false);
  assert.equal(isChatPanelMessage({ type: "persona.set" }), false);
  assert.equal(isChatPanelMessage({ type: "persona.set", name: "" }), false);
  assert.equal(isChatPanelMessage({ type: "persona.set", name: "has space" }), false);
  assert.equal(isChatPanelMessage({ type: "persona.set", name: "../traversal" }), false);
  assert.equal(isChatPanelMessage({ type: "persona.set", name: ".hidden" }), false);
  assert.equal(isChatPanelMessage({ type: "persona.set", name: `a${"b".repeat(64)}` }), false);
  assert.equal(isChatPanelMessage({ type: "profile.use" }), false);
  assert.equal(isChatPanelMessage({ type: "profile.use", name: "" }), false);
  assert.equal(isChatPanelMessage({ type: "submit", text: "x".repeat(20_001), requestId: "r" }), false);
  assert.equal(isChatPanelMessage({ type: "slash.quickAction", command: `/${"x".repeat(4_096)}`, requestId: "r" }), false);
  assert.equal(isChatPanelMessage({ type: "approval", approvalId: "x".repeat(1_025), decision: "deny" }), false);
  assert.equal(isChatPanelMessage({ type: "session.model.set", model: "x".repeat(513) }), false);
  assert.equal(isChatPanelMessage({ type: "browser.start", allow_local_destinations: true }), false);
  assert.equal(isChatPanelMessage({ type: "browser.start", executable_path: "chrome" }), false);
  assert.equal(isChatPanelMessage({ type: "browser.startLocal", network_scope: "public_loopback" }), false);
  assert.equal(isChatPanelMessage({ type: "browser.startLocal", confirm: true }), false);
  assert.equal(isChatPanelMessage({ type: "browser.navigate", url: "x".repeat(8_193) }), false);
  assert.equal(isChatPanelMessage({ type: "browser.snapshot", kind: "raw-cdp" }), false);
  assert.equal(isChatPanelMessage({ type: "browser.type", selector: "#field", text: "" }), false);
  assert.equal(isChatPanelMessage({ type: "browser.screenshot.save", destination: "C:/secret.png" }), false);
});

test("every message case is a closed schema: unexpected keys are rejected uniformly", () => {
  // Previously approval, artifact.open, profile.use, session.model.set, and mode.set accepted
  // extra keys. No handler spreads a message today, but the
  // validator is the boundary — it must not depend on that staying true.
  const withExtras: unknown[] = [
    { type: "approval", approvalId: "a1", decision: "deny", allow: true },
    { type: "artifact.open", artifactId: "a.txt", path: "/etc/passwd" },
    { type: "profile.use", name: "openai", apiKey: "sk-live" },
    { type: "session.model.set", model: "gpt-5", baseUrl: "https://evil.example" },
    { type: "persona.set", name: "architect", mode: "auto" },
    { type: "mode.set", mode: "readonly", sandbox: "none" },
    { type: "cancel", sessionId: "other-session" },
    { type: "bridge.restart", cliPath: "/tmp/evil" },
    { type: "cockpit.retryStatus", cliPath: "/tmp/evil" }
  ];

  for (const message of withExtras) {
    assert.equal(isChatPanelMessage(message), false, `accepted extra keys: ${JSON.stringify(message)}`);
  }
});
