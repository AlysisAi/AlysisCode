import assert from "node:assert/strict";
import Module from "node:module";
import test from "node:test";

const disposable = { dispose: () => undefined };
const registeredCommands = new Map<string, () => Promise<void>>();
const clipboardWrites: string[] = [];
const openedUrls: string[] = [];
const executedCommands: Array<{ command: string; args: unknown[] }> = [];
let executeShouldThrow = false;

const vscodeStub = {
  commands: {
    registerCommand: (command: string, callback: () => Promise<void>) => {
      registeredCommands.set(command, callback);
      return disposable;
    },
    executeCommand: async (command: string, ...args: unknown[]) => {
      executedCommands.push({ command, args });
      if (executeShouldThrow) {
        throw new Error("walkthrough unavailable");
      }
    }
  },
  env: {
    clipboard: {
      writeText: async (text: string) => {
        clipboardWrites.push(text);
      }
    },
    openExternal: async (uri: { toString(): string }) => {
      openedUrls.push(uri.toString());
      return true;
    }
  },
  window: {
    showInformationMessage: async () => undefined
  },
  Uri: {
    parse: (value: string) => ({ toString: () => value })
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

const { registerCliSetupCommands } = require("../src/commands/cliSetup") as typeof import("../src/commands/cliSetup");
moduleLoader._load = originalLoad;

test("CLI setup commands copy safe install and upgrade commands", async () => {
  registeredCommands.clear();
  clipboardWrites.length = 0;
  registerCliSetupCommands({ subscriptions: [] } as any);

  await registeredCommands.get("alysis.copyCliInstallCommand")?.();
  await registeredCommands.get("alysis.copyCliUpgradeCommand")?.();

  assert.deepEqual(clipboardWrites, [
    "pipx install alysis-code",
    "pipx upgrade alysis-code"
  ]);
  assert.doesNotMatch(clipboardWrites.join("\n"), /key|token|secret|sk-/i);
});

test("CLI setup guide command opens the Get Started walkthrough", async () => {
  registeredCommands.clear();
  openedUrls.length = 0;
  executedCommands.length = 0;
  executeShouldThrow = false;
  registerCliSetupCommands({ subscriptions: [] } as any);

  await registeredCommands.get("alysis.openSetupGuide")?.();

  assert.equal(executedCommands[0]?.command, "workbench.action.openWalkthrough");
  assert.equal(executedCommands[0]?.args[0], "alysisai.vscode-alysis#alysis.gettingStarted");
  assert.equal(openedUrls.length, 0);
});

test("CLI setup guide falls back to the docs URL when the walkthrough is unavailable", async () => {
  registeredCommands.clear();
  openedUrls.length = 0;
  executedCommands.length = 0;
  executeShouldThrow = true;
  registerCliSetupCommands({ subscriptions: [] } as any);

  await registeredCommands.get("alysis.openSetupGuide")?.();

  assert.equal(openedUrls[0], "https://github.com/AlysisAi/AlysisCode#install");
});
