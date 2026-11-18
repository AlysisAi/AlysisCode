export type ChatPanelMessage =
  | { type: "submit"; text: string; requestId: string }
  | { type: "slash.quickAction"; command: string; requestId: string }
  | { type: "cancel" }
  | { type: "approval"; approvalId: string; decision: "allow_once" | "allow_for_session" | "deny" }
  | { type: "artifact.refresh" }
  | { type: "artifact.open"; artifactId: string }
  | { type: "command.openSetupGuide" }
  | { type: "command.openOutput" }
  | { type: "command.setCliPath" }
  | { type: "cockpit.retryStatus" }
  | { type: "mode.set"; mode: "readonly" | "review" | "auto" | "fullaccess" }
  | { type: "session.model.set"; model: string }
  | { type: "persona.set"; name: string }
  | { type: "profile.use"; name: string }
  | { type: "bridge.restart" }
  | { type: "browser.refresh" }
  | { type: "browser.start" }
  | { type: "browser.startLocal" }
  | { type: "browser.select"; browserSessionId: string }
  | { type: "browser.navigate"; url: string }
  | { type: "browser.snapshot"; kind: "semantic" | "accessibility" | "dom" | "text" }
  | { type: "browser.screenshot"; fullPage?: boolean }
  | { type: "browser.diagnostics" }
  | { type: "browser.click"; selector: string }
  | { type: "browser.type"; selector: string; text: string; replace?: boolean }
  | { type: "browser.close" }
  | { type: "browser.screenshot.save" };

export function isChatPanelMessage(value: unknown): value is ChatPanelMessage {
  if (typeof value !== "object" || value === null || !("type" in value)) {
    return false;
  }
  const message = value as Record<string, unknown>;
  switch (message.type) {
    case "submit":
      return hasOnlyKeys(message, ["type", "text", "requestId"])
        && isNonEmptyString(message.text, 20_000)
        && isNonEmptyString(message.requestId, 128);
    case "slash.quickAction":
      return hasOnlyKeys(message, ["type", "command", "requestId"])
        && isNonEmptyString(message.command, 4_096)
        && message.command.trim().startsWith("/")
        && isNonEmptyString(message.requestId, 128);
    case "cancel":
    case "artifact.refresh":
    case "command.openSetupGuide":
    case "command.openOutput":
    case "command.setCliPath":
    case "cockpit.retryStatus":
    case "bridge.restart":
    case "browser.refresh":
    case "browser.start":
    case "browser.startLocal":
    case "browser.diagnostics":
    case "browser.close":
    case "browser.screenshot.save":
      return hasOnlyKeys(message, ["type"]);
    case "browser.select":
      return hasOnlyKeys(message, ["type", "browserSessionId"])
        && isNonEmptyString(message.browserSessionId, 64)
        && /^[A-Za-z0-9_-]+$/.test(message.browserSessionId);
    case "browser.navigate":
      return hasOnlyKeys(message, ["type", "url"])
        && isNonEmptyString(message.url, 8_192);
    case "browser.snapshot":
      return hasOnlyKeys(message, ["type", "kind"])
        && ["semantic", "accessibility", "dom", "text"].includes(String(message.kind));
    case "browser.screenshot":
      return hasOnlyKeys(message, ["type", "fullPage"])
        && (message.fullPage === undefined || typeof message.fullPage === "boolean");
    case "browser.click":
      return hasOnlyKeys(message, ["type", "selector"])
        && isNonEmptyString(message.selector, 2_000);
    case "browser.type":
      return hasOnlyKeys(message, ["type", "selector", "text", "replace"])
        && isNonEmptyString(message.selector, 2_000)
        && isNonEmptyString(message.text, 100_000)
        && (message.replace === undefined || typeof message.replace === "boolean");
    case "approval":
      return hasOnlyKeys(message, ["type", "approvalId", "decision"])
        && isNonEmptyString(message.approvalId, 1_024)
        && isApprovalDecision(message.decision);
    case "artifact.open":
      return hasOnlyKeys(message, ["type", "artifactId"]) && isNonEmptyString(message.artifactId, 1_024);
    case "profile.use":
      return hasOnlyKeys(message, ["type", "name"]) && isNonEmptyString(message.name, 512);
    case "session.model.set":
      return hasOnlyKeys(message, ["type", "model"]) && isNonEmptyString(message.model, 512);
    case "persona.set":
      return hasOnlyKeys(message, ["type", "name"]) && isPersonaName(message.name);
    case "mode.set":
      return (
        hasOnlyKeys(message, ["type", "mode"]) &&
        (message.mode === "readonly" ||
          message.mode === "review" ||
          message.mode === "auto" ||
          message.mode === "fullaccess")
      );
    default:
      return false;
  }
}

function isNonEmptyString(value: unknown, maxLength: number): value is string {
  return typeof value === "string" && value.trim().length > 0 && value.length <= maxLength;
}

function hasOnlyKeys(value: Record<string, unknown>, allowed: readonly string[]): boolean {
  const keys = new Set(allowed);
  return Object.keys(value).every((key) => keys.has(key));
}

function isApprovalDecision(value: unknown): value is "allow_once" | "allow_for_session" | "deny" {
  return value === "allow_once" || value === "allow_for_session" || value === "deny";
}

/** Persona names are CLI-profile-like: 64 chars max, no path or shell metacharacters. */
function isPersonaName(value: unknown): value is string {
  return typeof value === "string" && /^[a-z0-9][a-z0-9._-]{0,63}$/i.test(value);
}
