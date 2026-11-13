import assert from "node:assert/strict";
import { chmodSync, mkdirSync, mkdtempSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  CliDiscovery,
  CliRunner,
  ConfigInspect,
  ConfigReader,
  cliCommandEnvironment,
  evaluateCliExecution,
  getAlysisConfig,
  looksLikeMissingSubcommand,
  redactDeep,
  redactForDisplay,
  resolveCliExecutionPath,
  resolveCliPath
} from "../src/client/CliDiscovery";
import { resolveExecutablePath, unsupportedShimReason } from "../src/client/ExecutableResolver";
import {
  PROCESS_EXECUTION_COMMANDS,
  evaluateProcessExecutionCommand
} from "../src/security/commandGuards";
import { REQUIRED_BRIDGE_METHODS } from "../src/client/AlysisProtocol";

test("getAlysisConfig parses defaults and falls back to safe values", () => {
  const config = getAlysisConfig(reader({ defaultMode: "fullaccess", transport: "http" }));

  assert.equal(config.cliPath, "");
  assert.equal(config.defaultMode, "review");
  assert.equal(config.transport, "stdio");
  assert.equal(config.showStatusBar, true);
  assert.equal(resolveCliPath(config), "alysis");
});

test("getAlysisConfig parses configured CLI and mode", () => {
  const config = getAlysisConfig(
    reader({
      cliPath: "/opt/alysis/bin/alysis",
      defaultMode: "readonly",
      defaultModel: "deepseek-v4-pro",
      forgeExecuteMaxSteps: 12.8,
      forgeExecuteNoLog: true,
      showStatusBar: false
    })
  );

  assert.equal(resolveCliPath(config), "/opt/alysis/bin/alysis");
  assert.equal(config.defaultMode, "readonly");
  assert.equal(config.defaultModel, "deepseek-v4-pro");
  assert.equal(config.forgeExecuteMaxSteps, 12);
  assert.equal(config.forgeExecuteNoLog, true);
  assert.equal(config.showStatusBar, false);
});

test("getAlysisConfig leaves Forge Execute max_steps unset for zero or invalid values", () => {
  assert.equal(getAlysisConfig(reader({ forgeExecuteMaxSteps: 0 })).forgeExecuteMaxSteps, undefined);
  assert.equal(getAlysisConfig(reader({ forgeExecuteMaxSteps: -5 })).forgeExecuteMaxSteps, undefined);
  assert.equal(getAlysisConfig(reader({ forgeExecuteMaxSteps: "not-a-number" })).forgeExecuteMaxSteps, undefined);
});

test("getAlysisConfig never rewrites any provider's configured endpoint", () => {
  // No provider name is privileged: a "Xiaomi MiMo" provider with a custom base URL keeps that
  // base URL exactly like any other provider does.
  for (const provider of ["Xiaomi MiMo", "OpenAI-compatible", "Anthropic Claude"]) {
    const config = getAlysisConfig(
      reader({ provider, baseUrl: "https://gateway.example.test/v1" })
    );

    assert.equal(config.provider, provider);
    assert.equal(config.baseUrl, "https://gateway.example.test/v1");
  }
});

test("getAlysisConfig resolves host network settings and reports each rejected value once", () => {
  const notices: string[] = [];
  const config = getAlysisConfig(reader({ extraCaCerts: "relative/ca.pem" }), {
    hostNetwork: { proxy: "not a url", noProxy: ["localhost"], proxySupport: "on", proxyStrictSSL: false },
    onHostNetworkNotice: (notice) => notices.push(notice.code),
    fileExists: () => true
  });
  assert.deepEqual(config.hostNetwork, { proxySupport: "on", proxy: "", noProxy: ["localhost"], extraCaCerts: "" });
  assert.equal(config.extraCaCerts, "");
  assert.deepEqual(notices, ["proxy_invalid", "ca_bundle_relative", "strict_ssl_ignored"]);
});

test("getAlysisConfig keeps a valid proxy and CA bundle", () => {
  const config = getAlysisConfig(reader({ extraCaCerts: "/etc/pki/corp-ca.pem" }), {
    hostNetwork: { proxy: "http://proxy.corp.example:3128" },
    onHostNetworkNotice: () => assert.fail("no notice expected"),
    fileExists: (filePath) => filePath === "/etc/pki/corp-ca.pem"
  });
  assert.equal(config.hostNetwork?.proxy, "http://proxy.corp.example:3128");
  assert.equal(config.extraCaCerts, "/etc/pki/corp-ca.pem");
});

test("getAlysisConfig still validates extraCaCerts when the caller supplies no http settings", () => {
  const notices: string[] = [];
  const config = getAlysisConfig(reader({ extraCaCerts: "/etc/pki/corp-ca.pem" }), {
    onHostNetworkNotice: (notice) => notices.push(notice.code),
    fileExists: () => false
  });
  assert.deepEqual(config.hostNetwork, { proxySupport: "override", proxy: "", noProxy: [], extraCaCerts: "" });
  assert.equal(config.extraCaCerts, "");
  assert.deepEqual(notices, ["ca_bundle_missing"]);
});

test("getAlysisConfig leaves an unset baseUrl empty regardless of provider", () => {
  // Provider defaults come from the CLI's preset catalog, not from extension-side rewrites.
  for (const provider of ["Xiaomi MiMo", "OpenAI-compatible"]) {
    assert.equal(getAlysisConfig(reader({ provider })).baseUrl, "");
  }
});

test("CliDiscovery detects version and health success", async () => {
  const runner: CliRunner = async (_command, args) => {
    if (args.join(" ") === "--version") {
      return { stdout: "alysis 0.1.4\n", stderr: "", exitCode: 0 };
    }
    if (args.join(" ") === "ide-bridge health") {
      return { stdout: JSON.stringify(healthPayload()), stderr: "", exitCode: 0 };
    }
    throw new Error(`unexpected args: ${args.join(" ")}`);
  };

  const result = await new CliDiscovery(runner).detect(
    getAlysisConfig(reader({}), { executableResolver: { env: { PATH: "" } } })
  );

  assert.equal(result.ok, true);
  if (result.ok) {
    assert.equal(result.cliPath, "alysis");
    assert.equal(result.health.protocol_version, "1");
  }
});

test("CliDiscovery redacts credentials from successful version output", async () => {
  const runner: CliRunner = async (_command, args) => {
    if (args.join(" ") === "--version") {
      return {
        stdout: "alysis 0.1.4 Authorization: Bearer abcdefgh1234567890\n",
        stderr: "",
        exitCode: 0
      };
    }
    return { stdout: JSON.stringify(healthPayload()), stderr: "", exitCode: 0 };
  };

  const result = await new CliDiscovery(runner).detect(
    getAlysisConfig(reader({}), { executableResolver: { env: { PATH: "" } } })
  );

  assert.equal(result.ok, true);
  if (result.ok) {
    assert.match(result.versionOutput, /<redacted>/);
    assert.doesNotMatch(result.versionOutput, /abcdefgh1234567890/);
  }
});

test("sandbox doctor uses the session preference while preserving environment policy", () => {
  const keys = ["ALYSIS_SHELL_SANDBOX_MODE", "ALYSIS_VERIFY_SANDBOX_MODE"];
  const saved = keys.map((key) => process.env[key]);
  try {
    for (const key of keys) delete process.env[key];
    const config = getAlysisConfig(reader({ sandboxProfile: "warn" }));
    const env = cliCommandEnvironment(config);
    assert.equal(env.ALYSIS_SHELL_SANDBOX_MODE, "warn");
    assert.equal(env.ALYSIS_VERIFY_SANDBOX_MODE, "warn");
    assert.equal(process.env.ALYSIS_SHELL_SANDBOX_MODE, undefined);
    process.env.ALYSIS_SHELL_SANDBOX_MODE = "strict";
    process.env.ALYSIS_VERIFY_SANDBOX_MODE = "strict";
    const restricted = cliCommandEnvironment(config);
    assert.equal(restricted.ALYSIS_SHELL_SANDBOX_MODE, "strict");
    assert.equal(restricted.ALYSIS_VERIFY_SANDBOX_MODE, "strict");
  } finally {
    keys.forEach((key, index) => {
      if (saved[index] === undefined) delete process.env[key];
      else process.env[key] = saved[index];
    });
  }
});

test("CLI diagnostic environments never inherit provider credentials", () => {
  const previous = process.env.ALYSIS_API_KEY;
  const previousOpenAi = process.env.OPENAI_API_KEY;
  const previousGithub = process.env.GITHUB_TOKEN;
  const previousSshAgent = process.env.SSH_AUTH_SOCK;
  process.env.ALYSIS_API_KEY = "inherited-provider-secret";
  process.env.OPENAI_API_KEY = "openai-provider-secret";
  process.env.GITHUB_TOKEN = "github-secret";
  process.env.SSH_AUTH_SOCK = "safe-agent-socket-path";
  try {
    const environment = cliCommandEnvironment(getAlysisConfig(reader({})));
    assert.equal(environment.ALYSIS_API_KEY, undefined);
    assert.equal(environment.OPENAI_API_KEY, undefined);
    assert.equal(environment.GITHUB_TOKEN, undefined);
    assert.equal(environment.SSH_AUTH_SOCK, "safe-agent-socket-path");
    assert.equal(environment.PYTHONUTF8, "1");
    assert.equal(environment.PYTHONIOENCODING, "utf-8");
    assert.equal(process.env.ALYSIS_API_KEY, "inherited-provider-secret");
  } finally {
    if (previous === undefined) {
      delete process.env.ALYSIS_API_KEY;
    } else {
      process.env.ALYSIS_API_KEY = previous;
    }
    if (previousOpenAi === undefined) {
      delete process.env.OPENAI_API_KEY;
    } else {
      process.env.OPENAI_API_KEY = previousOpenAi;
    }
    if (previousGithub === undefined) {
      delete process.env.GITHUB_TOKEN;
    } else {
      process.env.GITHUB_TOKEN = previousGithub;
    }
    if (previousSshAgent === undefined) {
      delete process.env.SSH_AUTH_SOCK;
    } else {
      process.env.SSH_AUTH_SOCK = previousSshAgent;
    }
  }
});

test("CliDiscovery reports missing CLI", async () => {
  const runner: CliRunner = async () => {
    const error = new Error("spawn alysis ENOENT") as NodeJS.ErrnoException;
    error.code = "ENOENT";
    throw error;
  };

  const result = await new CliDiscovery(runner).detect(
    getAlysisConfig(reader({}), { executableResolver: { env: { PATH: "" } } })
  );

  assert.equal(result.ok, false);
  if (!result.ok) {
    assert.equal(result.code, "cli_missing");
  }
});

test("CliDiscovery redacts credentials from unexpected probe failures", async () => {
  const runner: CliRunner = async () => {
    throw new Error("probe failed with Authorization: Bearer abcdefgh1234567890");
  };

  const result = await new CliDiscovery(runner).detect(
    getAlysisConfig(reader({}), { executableResolver: { env: { PATH: "" } } })
  );

  assert.equal(result.ok, false);
  if (!result.ok) {
    assert.equal(result.code, "cli_error");
    assert.match(result.message, /<redacted>/);
    assert.doesNotMatch(result.message, /abcdefgh1234567890/);
  }
});

test("CliDiscovery reports invalid bridge health JSON", async () => {
  const runner: CliRunner = async (_command, args) => {
    if (args.join(" ") === "--version") {
      return { stdout: "alysis 0.1.4\n", stderr: "", exitCode: 0 };
    }
    return { stdout: "not json", stderr: "", exitCode: 0 };
  };

  const result = await new CliDiscovery(runner).detect(
    getAlysisConfig(reader({}), { executableResolver: { env: { PATH: "" } } })
  );

  assert.equal(result.ok, false);
  if (!result.ok) {
    assert.equal(result.code, "invalid_json");
  }
});

test("CliDiscovery reports old CLI without ide-bridge as incompatible", async () => {
  const runner: CliRunner = async (_command, args) => {
    if (args.join(" ") === "--version") {
      return { stdout: "alysis 0.1.0\n", stderr: "", exitCode: 0 };
    }
    return { stdout: "", stderr: "Error: No such command 'ide-bridge'.\n", exitCode: 2 };
  };

  const result = await new CliDiscovery(runner).detect(
    getAlysisConfig(reader({}), { executableResolver: { env: { PATH: "" } } })
  );

  assert.equal(result.ok, false);
  if (!result.ok) {
    assert.equal(result.code, "ide_bridge_missing");
    assert.match(result.message, /Upgrade alysis-code/);
  }
});

test("untrusted workspace ignores workspace-scoped cliPath and blocks execution", () => {
  const notices: string[] = [];
  const config = getAlysisConfig(
    inspectReader({
      cliPath: { defaultValue: "", workspaceValue: "./malicious-alysis" }
    }),
    {
      isWorkspaceTrusted: false,
      workspaceRoots: ["/workspace/project"],
      onNotice: (notice) => notices.push(notice.message)
    }
  );

  assert.equal(config.cliPath, "");
  assert.equal(resolveCliPath(config), "alysis");
  assert.equal(evaluateCliExecution(config).allowed, false);
  assert.match(evaluateCliExecution(config).reason ?? "", /Workspace-defined Alysis Code CLI path/);
  assert.equal(notices.some((notice) => notice.includes("Workspace-defined Alysis Code CLI path")), true);
});

test("untrusted workspace ignores workspace-scoped autoStartBridge", () => {
  const config = getAlysisConfig(
    inspectReader({
      autoStartBridge: { defaultValue: false, workspaceValue: true },
      forgeExecuteNoLog: { defaultValue: false, workspaceValue: true }
    }),
    { isWorkspaceTrusted: false }
  );

  assert.equal(config.autoStartBridge, false);
  assert.equal(config.security?.ignoredWorkspaceSettings.map((notice) => notice.setting).includes("autoStartBridge"), true);
  assert.equal(config.forgeExecuteNoLog, false);
  assert.equal(config.security?.ignoredWorkspaceSettings.map((notice) => notice.setting).includes("forgeExecuteNoLog"), true);
});

test("untrusted workspace rejects workspace-relative cliPath from trusted scopes", () => {
  const config = getAlysisConfig(
    inspectReader({
      cliPath: { defaultValue: "", globalValue: "./tools/alysis" }
    }),
    { isWorkspaceTrusted: false, workspaceRoots: ["/workspace/project"] }
  );

  const guard = evaluateCliExecution(config);
  assert.equal(resolveCliPath(config), "./tools/alysis");
  assert.equal(guard.allowed, false);
  assert.match(guard.reason ?? "", /Workspace-relative/);
});

test("untrusted workspace rejects absolute cliPath inside the workspace", () => {
  const config = getAlysisConfig(
    inspectReader({
      cliPath: { defaultValue: "", globalValue: "/workspace/project/.dev/alysis-dev" }
    }),
    { isWorkspaceTrusted: false, workspaceRoots: ["/workspace/project"] }
  );

  const guard = evaluateCliExecution(config);
  assert.equal(guard.allowed, false);
  assert.match(guard.reason ?? "", /Workspace-local/);
});

test("untrusted workspace blocks default PATH resolution inside workspace", () => {
  const workspace = mkdtempSync(join(tmpdir(), "alysis-workspace-"));
  const bin = join(workspace, "bin");
  const executable = join(bin, "alysis");
  mkdirSync(bin);
  writeFileSync(executable, "#!/bin/sh\n");
  chmodSync(executable, 0o755);

  const config = getAlysisConfig(reader({}), {
    isWorkspaceTrusted: false,
    workspaceRoots: [workspace],
    executableResolver: { env: { PATH: bin } }
  });

  const guard = evaluateCliExecution(config);
  assert.equal(guard.allowed, false);
  assert.equal(config.security?.cliPath.apiKeyForwardingAllowed, false);
  assert.match(guard.reason ?? "", /PATH-resolved.*Workspace Trust/);
});

test("untrusted workspace allows default PATH resolution outside workspace", () => {
  const workspace = mkdtempSync(join(tmpdir(), "alysis-workspace-"));
  const external = mkdtempSync(join(tmpdir(), "alysis-external-"));
  const executable = join(external, "alysis");
  writeFileSync(executable, "#!/bin/sh\n");
  chmodSync(executable, 0o755);

  const config = getAlysisConfig(reader({}), {
    isWorkspaceTrusted: false,
    workspaceRoots: [workspace],
    executableResolver: { env: { PATH: external } }
  });

  const guard = evaluateCliExecution(config);
  assert.equal(guard.allowed, true);
  assert.equal(config.security?.cliPath.apiKeyForwardingAllowed, true);
  assert.equal(resolveCliExecutionPath(config), realpathSync.native(executable));
});

test("untrusted unresolved default CLI strips API key forwarding but preserves execution diagnostics", () => {
  const config = getAlysisConfig(reader({}), {
    isWorkspaceTrusted: false,
    workspaceRoots: ["/workspace/project"],
    executableResolver: { env: { PATH: "" } }
  });

  const guard = evaluateCliExecution(config);
  assert.equal(guard.allowed, true);
  assert.equal(config.security?.cliPath.apiKeyForwardingAllowed, false);
  assert.equal(resolveCliExecutionPath(config), "alysis");
});

test("trusted workspace may use workspace-scoped cliPath", () => {
  const config = getAlysisConfig(
    inspectReader(
      {
        cliPath: { defaultValue: "", workspaceValue: "./.dev/alysis-dev" }
      },
      { cliPath: "./.dev/alysis-dev" }
    ),
    { isWorkspaceTrusted: true, workspaceRoots: ["/workspace/project"] }
  );

  assert.equal(resolveCliPath(config), "./.dev/alysis-dev");
  assert.equal(evaluateCliExecution(config).allowed, true);
  assert.equal(config.security?.cliPath.apiKeyForwardingAllowed, true);
});

test("process command guards block unsafe cliPath independent of package enablement", () => {
  const config = getAlysisConfig(
    inspectReader({
      cliPath: { defaultValue: "", workspaceValue: "./malicious-alysis" }
    }),
    { isWorkspaceTrusted: false }
  );

  for (const command of PROCESS_EXECUTION_COMMANDS) {
    assert.equal(evaluateProcessExecutionCommand(config, command).allowed, false, command);
  }
});

test("CliDiscovery detect does not execute an untrusted cliPath", async () => {
  let called = false;
  const config = getAlysisConfig(
    inspectReader({
      cliPath: { defaultValue: "", workspaceValue: "./malicious-alysis" }
    }),
    { isWorkspaceTrusted: false }
  );
  const result = await new CliDiscovery(async () => {
    called = true;
    return { stdout: "", stderr: "", exitCode: 0 };
  }).detect(config);

  assert.equal(called, false);
  assert.equal(result.ok, false);
  if (!result.ok) {
    assert.equal(result.code, "cli_untrusted");
  }
});

test("Windows PATHEXT resolution is covered without invoking a shell", () => {
  const seen = new Set(["C:\\Tools\\alysis.CMD".toLowerCase()]);
  const resolved = resolveExecutablePath("alysis", {
    platform: "win32",
    pathDelimiter: ";",
    env: { PATH: "C:\\Tools", PATHEXT: ".EXE;.CMD" },
    isExecutableFile: (candidate) => seen.has(candidate.toLowerCase())
  });

  assert.equal(resolved.resolvedPath, "C:\\Tools\\alysis.CMD");
});

test("PATH resolution drops empty entries instead of searching the working directory", () => {
  const probed: string[] = [];
  const resolved = resolveExecutablePath("alysis", {
    platform: "win32",
    pathDelimiter: ";",
    cwd: "C:\\workspace\\untrusted-repo",
    env: { PATH: ";;C:\\Tools;", PATHEXT: ".EXE" },
    isExecutableFile: (candidate) => {
      probed.push(candidate);
      return false;
    }
  });

  assert.equal(resolved.resolvedPath, null);
  assert.deepEqual(probed, ["C:\\Tools\\alysis.EXE", "C:\\Tools\\alysis"]);
  assert.equal(
    probed.some((candidate) => candidate.toLowerCase().includes("untrusted-repo")),
    false,
    "an empty PATH entry must never resolve to the current working directory"
  );
});

test("quoted Windows PATH entries resolve to the directory they name", () => {
  const seen = new Set(["C:\\Program Files\\Alysis Code\\alysis.EXE".toLowerCase()]);
  const resolved = resolveExecutablePath("alysis", {
    platform: "win32",
    pathDelimiter: ";",
    env: { PATH: "\"C:\\Program Files\\Alysis Code\"", PATHEXT: ".EXE" },
    isExecutableFile: (candidate) => seen.has(candidate.toLowerCase())
  });

  assert.equal(resolved.resolvedPath, "C:\\Program Files\\Alysis Code\\alysis.EXE");
});

test("Windows .cmd/.bat shims are reported as unspawnable instead of failing with EINVAL", () => {
  const reason = unsupportedShimReason("C:\\Tools\\alysis.CMD", "win32");
  assert.match(reason ?? "", /\.cmd wrapper/i);
  assert.match(reason ?? "", /alysis\.cliPath/);
  assert.match(reason ?? "", /EINVAL/);
  assert.match(unsupportedShimReason("C:\\Tools\\alysis.bat", "win32") ?? "", /\.bat wrapper/i);
  assert.equal(unsupportedShimReason("C:\\Tools\\alysis.exe", "win32"), null);
  assert.equal(unsupportedShimReason("/usr/local/bin/alysis.cmd", "linux"), null);
});

test("missing-subcommand classification survives a localized CLI through exit code and markers", () => {
  // English (unchanged behaviour).
  assert.equal(looksLikeMissingSubcommand("Error: No such command 'ide-bridge'.", "ide-bridge", 2), true);
  // Localized argparse still echoes the rejected argument verbatim and exits with the usage status.
  assert.equal(
    looksLikeMissingSubcommand("usage: alysis\nalysis: σφάλμα: μη έγκυρη επιλογή: 'ide-bridge'", "ide-bridge", 2),
    true
  );
  // A machine-readable marker classifies with no natural-language text at all.
  assert.equal(looksLikeMissingSubcommand('{"error":"unknown_command"}', "ide-bridge"), true);
  // A real failure of an existing subcommand must not be reclassified as "CLI too old".
  assert.equal(looksLikeMissingSubcommand("ide-bridge health failed: provider unreachable", "ide-bridge", 1), false);
  assert.equal(looksLikeMissingSubcommand("unrelated failure", "ide-bridge", 2), false);
});

test("redactForDisplay redacts bearer and sk-style secrets", () => {
  assert.equal(redactForDisplay("Authorization: Bearer abcdefgh1234567890"), "Authorization: <redacted>");
  assert.equal(redactForDisplay("token Bearer abcdefgh1234567890"), "token Bearer <redacted>");
  assert.equal(redactForDisplay("sk-abcdefghijklmnop"), "<redacted>");
  assert.equal(redactForDisplay("api_key=barecredentialvalue"), "api_key=<redacted>");
  assert.equal(
    redactForDisplay('{"client_secret":"anotherbarecredential"}'),
    '{"client_secret":"<redacted>"}'
  );
  assert.equal(
    redactForDisplay("https://example.test/callback?token=urlcredentialvalue&mode=ok"),
    "https://example.test/callback?token=<redacted>&mode=ok"
  );
});

test("redactDeep redacts secrets at every depth while preserving structure and non-strings", () => {
  const redacted = redactDeep({
    model: "opus",
    stream: true,
    count: 7,
    api_key: "sk-abcdefghijklmnop",
    headers: { authorization: "Bearer abcdefgh1234567890" },
    profiles: [{ name: "default", token: "sk-zzzzzzzzzzzz9999" }]
  }) as any;
  assert.equal(redacted.model, "opus");
  assert.equal(redacted.stream, true, "booleans pass through unchanged");
  assert.equal(redacted.count, 7, "numbers pass through unchanged");
  assert.equal(redacted.api_key, "<redacted>");
  assert.match(redacted.headers.authorization, /<redacted>/);
  assert.equal(redacted.profiles[0].name, "default");
  assert.equal(redacted.profiles[0].token, "<redacted>");
  assert.doesNotMatch(JSON.stringify(redacted), /sk-[A-Za-z0-9]{12}/, "no raw secret survives anywhere");
  // Primitive and null inputs are returned unchanged.
  assert.equal(redactDeep(42), 42);
  assert.equal(redactDeep(null), null);
});

test("redactDeep redacts a bare-token string value under a sensitive key (defense-in-depth)", () => {
  const redacted = redactDeep({
    password: "hunter2longpassword",
    api_secret: "AKIAEXAMPLELONGTOKEN1234",
    authorization: "rawbearertokenvalue12345",
    nested: { client_secret: "nopatternsecretvalue999" },
    // A sensitive key whose value is NOT a string (e.g. config.get's api_key metadata Record) must
    // recurse normally, not be blanked — it is not itself the secret.
    api_key: { present: true, source: "env" },
    model: "opus"
  }) as any;
  assert.equal(redacted.password, "<redacted>");
  assert.equal(redacted.api_secret, "<redacted>");
  assert.equal(redacted.authorization, "<redacted>");
  assert.equal(redacted.nested.client_secret, "<redacted>");
  assert.deepEqual(redacted.api_key, { present: true, source: "env" }, "non-string sensitive values recurse, not blanked");
  assert.equal(redacted.model, "opus", "non-sensitive fields are untouched");
});

function reader(values: Record<string, unknown>): ConfigReader {
  return {
    get<T>(section: string, defaultValue: T): T {
      return (section in values ? values[section] : defaultValue) as T;
    }
  };
}

function inspectReader(
  inspections: Record<string, ConfigInspect<unknown>>,
  values: Record<string, unknown> = {}
): ConfigReader {
  return {
    get<T>(section: string, defaultValue: T): T {
      return (section in values ? values[section] : defaultValue) as T;
    },
    inspect<T>(section: string): ConfigInspect<T> | undefined {
      return inspections[section] as ConfigInspect<T> | undefined;
    }
  };
}

function healthPayload(): Record<string, unknown> {
  return {
    ok: true,
    name: "alysis-ide-bridge",
    alysis_version: "0.1.4",
    protocol_version: "1",
    capabilities: {
      protocol_version: "1",
      methods: [...REQUIRED_BRIDGE_METHODS],
      events: ["message_delta"],
      modes: ["readonly", "review", "auto"],
      transport: "stdio-jsonl"
    }
  };
}
