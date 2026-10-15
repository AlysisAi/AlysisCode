import assert from "node:assert/strict";
import Module from "node:module";
import test from "node:test";

import { CockpitRuntimeState } from "../src/chat/CockpitRuntimeState";

const disposable = { dispose: () => undefined };
const registeredCommands = new Map<string, () => Promise<void>>();
const infos: string[] = [];
const warnings: string[] = [];
const errors: string[] = [];

const vscodeStub = {
  ProgressLocation: { SourceControl: 1, Window: 10, Notification: 15 },
  commands: {
    registerCommand: (command: string, callback: () => Promise<void>) => {
      registeredCommands.set(command, callback);
      return disposable;
    }
  },
  window: {
    withProgress: async (_options: unknown, task: () => Promise<unknown>) => task(),
    showInformationMessage: async (message: string) => {
      infos.push(message);
      return undefined;
    },
    showWarningMessage: async (message: string) => {
      warnings.push(message);
      return undefined;
    },
    showErrorMessage: async (message: string) => {
      errors.push(message);
      return undefined;
    }
  }
};

const moduleLoader = Module as unknown as {
  _load: (request: string, parent: NodeModule | null, isMain: boolean) => unknown;
};
const originalLoad = moduleLoader._load;
moduleLoader._load = function patchedLoad(request: string, parent: NodeModule | null, isMain: boolean): unknown {
  if (request === "vscode") {
    return vscodeStub;
  }
  return originalLoad.call(this, request, parent, isMain);
};

const { registerRunDoctorCommand, registerShowBridgeHealthCommand } = require("../src/commands/runDoctor") as typeof import("../src/commands/runDoctor");
moduleLoader._load = originalLoad;

for (const command of ["alysis.runDoctor", "alysis.showBridgeHealth"]) {
  test(`${command} waits for initial runtime validation before reading config or probing`, async () => {
    reset();
    let release!: () => void;
    const ready = new Promise<void>(resolve => { release = resolve; });
    let reads = 0;
    let probes = 0;
    const discovery = {
      runDoctor: async () => { probes++; return { stdout: "ok", stderr: "", exitCode: 0 }; },
      detect: async () => { probes++; return { ok: true, health: { protocol_version: "1", alysis_version: "test" } }; }
    };
    const register = command === "alysis.runDoctor" ? registerRunDoctorCommand : registerShowBridgeHealthCommand;
    register(context(), discovery as any, () => { reads++; return config(); }, output(), statusBar(), undefined, () => ready);
    const pending = registeredCommands.get(command)!();
    await Promise.resolve();
    assert.equal(reads, 0);
    assert.equal(probes, 0);
    assert.deepEqual(warnings, []);
    release();
    await pending;
    assert.equal(reads, 1);
    assert.equal(probes, 1);
    assert.deepEqual(errors, []);
  });
}

test("/doctor success appears in Timeline and Diagnostics runtime state", async () => {
  reset();
  const runtime = new CockpitRuntimeState();
  registerRunDoctorCommand(
    context(),
    {
      runDoctor: async () => ({ stdout: "doctor ok\n", stderr: "", exitCode: 0 })
    } as any,
    config,
    output(),
    statusBar(),
    runtime
  );

  await registeredCommands.get("alysis.runDoctor")?.();

  const snapshot = runtime.snapshot();
  assert.equal(snapshot.sandboxDoctor.status, "ok");
  assert.equal(snapshot.cliHealth.status, "ok");
  assert.equal(snapshot.events.some((event) => event.title === "Doctor started" && event.command === "alysis sandbox doctor --smoke"), true);
  assert.equal(snapshot.events.some((event) => event.title === "Doctor completed" && event.exitCode === 0 && event.stdout?.includes("doctor ok")), true);
});

test("/doctor failure appears in Timeline and Diagnostics with redacted stdio", async () => {
  reset();
  const runtime = new CockpitRuntimeState();
  registerRunDoctorCommand(
    context(),
    {
      runDoctor: async () => ({
        stdout: "using sk-test-secret-value\n",
        stderr: "authorization: Bearer abcdefghijklmnop\nfailed\n",
        exitCode: 2
      })
    } as any,
    config,
    output(),
    statusBar(),
    runtime
  );

  await registeredCommands.get("alysis.runDoctor")?.();

  const snapshot = runtime.snapshot();
  const failure = snapshot.events.find((event) => event.title === "Doctor failed");
  assert.equal(snapshot.sandboxDoctor.status, "failed");
  assert.equal(snapshot.cliHealth.status, "broken");
  assert.ok(failure);
  assert.equal(failure?.exitCode, 2);
  assert.doesNotMatch(`${failure?.stdout}\n${failure?.stderr}`, /sk-test-secret-value|abcdefghijklmnop/);
  assert.match(`${failure?.stdout}\n${failure?.stderr}`, /<redacted>/);
});

test("/doctor keeps a working CLI healthy when Docker is stopped", async () => {
  reset();
  const runtime = new CockpitRuntimeState();
  registerRunDoctorCommand(
    context(),
    { runDoctor: async () => ({
      stdout: "Docker daemon failed: failed to connect to the docker API at npipe:////./pipe/docker_engine",
      stderr: "", exitCode: 1
    }) } as any,
    config, output(), statusBar(), runtime
  );
  await registeredCommands.get("alysis.runDoctor")?.();
  const snapshot = runtime.snapshot();
  assert.equal(snapshot.cliHealth.status, "ok");
  assert.equal(snapshot.sandboxDoctor.status, "failed");
  assert.match(errors[0], /Start Docker/);
  assert.doesNotMatch(errors[0], /exit code|npipe|upgrade/);
});

test("/doctor missing sandbox subcommand reports old or incomplete CLI", async () => {
  reset();
  const runtime = new CockpitRuntimeState();
  registerRunDoctorCommand(
    context(),
    {
      runDoctor: async () => ({
        stdout: "",
        stderr: "Error: No such command 'doctor'.\n",
        exitCode: 2
      })
    } as any,
    config,
    output(),
    statusBar(),
    runtime
  );

  await registeredCommands.get("alysis.runDoctor")?.();

  const snapshot = runtime.snapshot();
  const failure = snapshot.events.find((event) => event.title === "Doctor failed");
  assert.equal(snapshot.sandboxDoctor.status, "failed");
  assert.equal(snapshot.cliHealth.status, "broken");
  assert.match(failure?.message ?? "", /does not support `alysis sandbox doctor --smoke`/);
  assert.match(errors[0] ?? "", /pipx upgrade alysis-code/);
});

test("/doctor redacts credentials from thrown CLI errors", async () => {
  reset();
  const runtime = new CockpitRuntimeState();
  registerRunDoctorCommand(
    context(),
    {
      runDoctor: async () => {
        throw new Error("doctor crashed with Authorization: Bearer abcdefgh1234567890");
      }
    } as any,
    config,
    output(),
    statusBar(),
    runtime
  );

  await registeredCommands.get("alysis.runDoctor")?.();

  const combined = `${errors.join("\n")}\n${JSON.stringify(runtime.snapshot())}`;
  assert.match(combined, /<redacted>/);
  assert.doesNotMatch(combined, /abcdefgh1234567890/);
});

function reset(): void {
  registeredCommands.clear();
  infos.length = 0;
  warnings.length = 0;
  errors.length = 0;
}

function context(): any {
  return { subscriptions: [] };
}

function output(): any {
  return {
    clear: () => undefined,
    appendLine: () => undefined,
    show: () => undefined
  };
}

function statusBar(): any {
  return {
    setError: () => undefined,
    setBridgeOk: () => undefined,
    setMissingCli: () => undefined,
    setIdle: () => undefined,
    snapshot: () => ({ state: "idle", tooltip: "Alysis Code is ready" })
  };
}

function config(): any {
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
        resolvedExecutablePath: "/usr/bin/alysis",
        source: "default",
        trusted: true,
        executionAllowed: true,
        apiKeyForwardingAllowed: true
      }
    }
  };
}
