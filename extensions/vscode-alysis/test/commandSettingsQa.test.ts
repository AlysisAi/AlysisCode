import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

import {
  ConfigInspect,
  ConfigReader,
  SENSITIVE_ALYSIS_SETTINGS,
  getAlysisConfig,
  resolveCliPath
} from "../src/client/CliDiscovery";
import { COMMANDS } from "../src/commands/registry";

const ALL_COMMANDS: readonly string[] = Object.values(COMMANDS);

const packageRoot = resolve(__dirname, "../..");
const packageJson = JSON.parse(readFileSync(resolve(packageRoot, "package.json"), "utf8")) as {
  activationEvents?: string[];
  contributes: {
    commands: Array<{ command: string; title: string; category?: string; enablement?: string }>;
    configuration: {
      properties: Record<string, {
        type?: string;
        default?: unknown;
        description?: string;
        markdownDescription?: string;
        enum?: unknown[];
        minimum?: number;
      }>;
    };
  };
  capabilities: { untrustedWorkspaces?: { restrictedConfigurations?: string[] } };
};

for (const commandId of ALL_COMMANDS) {
  test(`QA command contract: ${commandId}`, () => {
    const contributions = packageJson.contributes.commands.filter((item) => item.command === commandId);
    assert.equal(contributions.length, 1, `${commandId} must be contributed exactly once`);
    assert.equal(contributions[0].category, "Alysis Code");
    assert.ok(contributions[0].title.trim().length > 0);
    assert.equal(contributions[0].title.startsWith("Alysis Code:"), false);
  });
}

test("QA settings manifest has the complete unique schema", () => {
  const properties = packageJson.contributes.configuration.properties;
  const expected = [
    "alysis.cliPath",
    "alysis.defaultMode",
    "alysis.defaultModel",
    "alysis.baseUrl",
    "alysis.provider",
    "alysis.transport",
    "alysis.sandboxProfile",
    "alysis.showStatusBar",
    "alysis.autoStartBridge",
    "alysis.extraCaCerts"
  ];
  assert.deepEqual(Object.keys(properties).sort(), expected.sort());
  for (const key of expected) {
    const setting = properties[key];
    assert.ok(setting.type, `${key} type`);
    assert.ok("default" in setting, `${key} default`);
    assert.ok((setting.description ?? setting.markdownDescription ?? "").trim(), `${key} description`);
  }
});

test("QA cliPath empty default resolves through PATH", () => {
  const config = getAlysisConfig(reader({}));
  assert.equal(config.cliPath, "");
  assert.equal(resolveCliPath(config), "alysis");
});

test("QA cliPath trims a configured absolute path", () => {
  assert.equal(getAlysisConfig(reader({ cliPath: "  C:\\Tools\\alysis.exe  " })).cliPath, "C:\\Tools\\alysis.exe");
});

test("QA untrusted workspace ignores workspace-local cliPath", () => {
  const config = getAlysisConfig(
    inspectReader({ cliPath: { defaultValue: "", workspaceValue: ".\\workspace-cli.exe" } }),
    { isWorkspaceTrusted: false, workspaceRoots: ["C:\\workspace"] }
  );
  assert.equal(config.cliPath, "");
  assert.equal(config.security?.cliPath.executionAllowed, false);
});

for (const mode of ["readonly", "review", "auto"] as const) {
  test(`QA defaultMode accepts ${mode}`, () => {
    assert.equal(getAlysisConfig(reader({ defaultMode: mode })).defaultMode, mode);
  });
}

test("QA defaultMode rejects malformed values", () => {
  assert.equal(getAlysisConfig(reader({ defaultMode: "fullaccess" })).defaultMode, "review");
  assert.equal(getAlysisConfig(reader({ defaultMode: 42 })).defaultMode, "review");
});

test("QA defaultModel trims normal and preserves empty values", () => {
  assert.equal(getAlysisConfig(reader({ defaultModel: "  mimo-v2.5-pro  " })).defaultModel, "mimo-v2.5-pro");
  assert.equal(getAlysisConfig(reader({})).defaultModel, "");
});

test("QA defaultModel handles the 512 character boundary", () => {
  const model = "m".repeat(512);
  assert.equal(getAlysisConfig(reader({ defaultModel: model })).defaultModel, model);
});

test("QA baseUrl trims a valid endpoint", () => {
  assert.equal(
    getAlysisConfig(reader({ baseUrl: "  https://api.xiaomimimo.com/v1  " })).baseUrl,
    "https://api.xiaomimimo.com/v1"
  );
});

for (const [label, local] of [
  ["localhost", "http://localhost:11434/v1"],
  ["127.0.0.1", "http://127.0.0.1:1234/v1"],
  ["127.x", "http://127.0.0.2:8080/v1"],
  ["ipv6 loopback", "http://[::1]:11434/v1"]
] as const) {
  test(`QA baseUrl accepts plain http for loopback host: ${label}`, () => {
    assert.equal(getAlysisConfig(reader({ baseUrl: local })).baseUrl, local);
  });
}

for (const [label, invalid] of [
  ["relative", "not-a-url"],
  ["non-http", "ftp://example.test/v1"],
  ["plain http to a remote host", "http://api.example.test/v1"],
  ["plain http to a LAN host", "http://192.168.1.20:8080/v1"],
  ["plain http to a lookalike", "http://localhost.example.test/v1"],
  ["userinfo", "https://user:password@example.test/v1"],
  ["query", "https://example.test/v1?token=hidden"],
  ["fragment", "https://example.test/v1#fragment"],
  ["whitespace", "https://example.test/v1 path"]
] as const) {
  test(`QA baseUrl rejects malformed endpoint: ${label}`, () => {
    assert.equal(getAlysisConfig(reader({ baseUrl: invalid })).baseUrl, "");
  });
}

test("QA provider trims normal and preserves empty values", () => {
  assert.equal(getAlysisConfig(reader({ provider: "  Xiaomi MiMo  " })).provider, "Xiaomi MiMo");
  assert.equal(getAlysisConfig(reader({})).provider, "");
});

test("QA transport accepts stdio and rejects malformed values", () => {
  assert.equal(getAlysisConfig(reader({ transport: "stdio" })).transport, "stdio");
  assert.equal(getAlysisConfig(reader({ transport: "http" })).transport, "stdio");
  assert.equal(getAlysisConfig(reader({ transport: null })).transport, "stdio");
});

for (const profile of ["default", "strict", "warn", "off"] as const) {
  test(`QA sandboxProfile accepts ${profile}`, () => {
    assert.equal(getAlysisConfig(reader({ sandboxProfile: profile })).sandboxProfile, profile);
  });
}

test("QA sandboxProfile rejects malformed values", () => {
  assert.equal(getAlysisConfig(reader({ sandboxProfile: "dangerous" })).sandboxProfile, "default");
  assert.equal(getAlysisConfig(reader({ sandboxProfile: null })).sandboxProfile, "default");
});

for (const [setting, defaultValue] of [
  ["showStatusBar", true],
  ["autoStartBridge", false]
] as const) {
  test(`QA ${setting} accepts booleans and rejects malformed values`, () => {
    assert.equal((getAlysisConfig(reader({ [setting]: true })) as unknown as Record<string, unknown>)[setting], true);
    assert.equal((getAlysisConfig(reader({ [setting]: false })) as unknown as Record<string, unknown>)[setting], false);
    assert.equal((getAlysisConfig(reader({ [setting]: "false" })) as unknown as Record<string, unknown>)[setting], defaultValue);
    assert.equal((getAlysisConfig(reader({ [setting]: 1 })) as unknown as Record<string, unknown>)[setting], defaultValue);
  });
}

test("QA every sensitive setting is restricted and ignored from untrusted workspace scope", () => {
  const restricted = new Set(packageJson.capabilities.untrustedWorkspaces?.restrictedConfigurations ?? []);
  for (const setting of SENSITIVE_ALYSIS_SETTINGS) {
    assert.equal(restricted.has(`alysis.${setting}`), true, setting);
  }
  const inspected = Object.fromEntries(
    SENSITIVE_ALYSIS_SETTINGS.map((setting) => [setting, { defaultValue: undefined, workspaceValue: "malformed-workspace-value" }])
  );
  const config = getAlysisConfig(inspectReader(inspected), { isWorkspaceTrusted: false });
  assert.equal(config.security?.ignoredWorkspaceSettings.length, SENSITIVE_ALYSIS_SETTINGS.length);
});

test("QA showStatusBar is intentionally unrestricted in untrusted workspaces", () => {
  const restricted = new Set(packageJson.capabilities.untrustedWorkspaces?.restrictedConfigurations ?? []);
  assert.equal(restricted.has("alysis.showStatusBar"), false);
  assert.equal(getAlysisConfig(reader({ showStatusBar: false }), { isWorkspaceTrusted: false }).showStatusBar, false);
});

test("QA live configuration change refreshes all visible consumers", () => {
  const source = readFileSync(resolve(packageRoot, "src/extension.ts"), "utf8");
  const handler = source.match(/onDidChangeConfiguration\(\(event\) => \{[\s\S]*?\n\s*\}\)\n\s*\)/)?.[0] ?? source;
  assert.match(handler, /affectsConfiguration\("alysis"\)/);
  assert.match(handler, /getConfig\(\)/);
  assert.match(handler, /runtime\.applyConfig\(config\)/);
  assert.match(handler, /applyStatusBarVisibility\(statusBar, config\)/);
  assert.match(handler, /profileStore\.refresh\(bridgeClient\)/);
  assert.match(handler, /chatController\.refreshCockpit\(\)/);
  assert.match(handler, /startView\?\.update\(\)/);
});

test("QA trust grant re-reads autoStartBridge before optional health check", () => {
  const source = readFileSync(resolve(packageRoot, "src/extension.ts"), "utf8");
  assert.match(source, /onDidGrantWorkspaceTrust\(\(\) => \{[\s\S]*?const config = getConfig\(\);[\s\S]*?if \(config\.autoStartBridge\) \{[\s\S]*?checkBridgeStatus\(\)/);
});

test("QA command registry and manifest contain no duplicates and match exactly", () => {
  const manifestIds = packageJson.contributes.commands.map((item) => item.command);
  assert.equal(new Set(ALL_COMMANDS).size, ALL_COMMANDS.length);
  assert.equal(new Set(manifestIds).size, manifestIds.length);
  assert.deepEqual(new Set(manifestIds), new Set(ALL_COMMANDS));
});

test("QA command and view activation stays lazy", () => {
  // VS Code >= 1.74 derives activation events from `contributes`, so every contributed command,
  // view, and chat participant activates lazily without an explicit entry. Listing them again is
  // noise that hides the events that genuinely matter, so the manifest declares only the internal
  // command that has no contribution to derive from.
  const activation = new Set(packageJson.activationEvents ?? []);
  assert.equal(activation.has("onStartupFinished"), false);
  assert.equal(activation.has("*"), false);
  for (const command of ALL_COMMANDS) {
    assert.equal(activation.has(`onCommand:${command}`), false, `${command} activates implicitly`);
  }
  assert.deepEqual([...activation], ["onCommand:alysis.runtimeEvidence"]);
});

function reader(values: Record<string, unknown>): ConfigReader {
  return {
    get<T>(section: string, defaultValue: T): T {
      return Object.prototype.hasOwnProperty.call(values, section)
        ? values[section] as T
        : defaultValue;
    }
  };
}

function inspectReader(values: Record<string, ConfigInspect<unknown>>): ConfigReader {
  return {
    get<T>(section: string, defaultValue: T): T {
      const inspected = values[section];
      if (!inspected) {
        return defaultValue;
      }
      return (inspected.workspaceFolderLanguageValue
        ?? inspected.workspaceLanguageValue
        ?? inspected.workspaceFolderValue
        ?? inspected.workspaceValue
        ?? inspected.globalLanguageValue
        ?? inspected.globalValue
        ?? inspected.defaultLanguageValue
        ?? inspected.defaultValue
        ?? defaultValue) as T;
    },
    inspect<T>(section: string): ConfigInspect<T> | undefined {
      return values[section] as ConfigInspect<T> | undefined;
    }
  };
}
