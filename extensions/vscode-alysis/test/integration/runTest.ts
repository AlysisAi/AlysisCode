import { accessSync, chmodSync, constants, cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { Socket } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { runVsCodeExtensionHostTests } from "./vsCodeExtensionHostInvocation";
import { assertSuccessfulExtensionHostResult } from "./extensionHostResult";
import { copyTestDependencyTree } from "./copyTestDependencyTree";

const vscodeDownloadHost = "update.code.visualstudio.com";
const extensionHostDownloadFailure =
  "Extension Host could not run because VS Code executable was not provided and download failed/unavailable.";

function removeTempDirectory(path: string): void {
  try {
    rmSync(path, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    console.warn(`Could not remove Extension Host test directory ${path}: ${reason}`);
  }
}

function verifyExecutable(path: string): void {
  const stats = (() => {
    try {
      return statSync(path);
    } catch (error) {
      throw new Error(`VSCODE_TEST_EXECUTABLE_PATH does not exist: ${path}`, { cause: error });
    }
  })();
  if (!stats.isFile()) {
    throw new Error(`VSCODE_TEST_EXECUTABLE_PATH is not a file: ${path}`);
  }
  if (process.platform !== "win32") {
    try {
      accessSync(path, constants.X_OK);
    } catch (error) {
      throw new Error(`VSCODE_TEST_EXECUTABLE_PATH is not executable: ${path}`, { cause: error });
    }
  }
}

async function preflightDownload(timeoutMs = 5000): Promise<void> {
  await new Promise<void>((resolvePromise, reject) => {
    const socket = new Socket();
    let settled = false;
    const finish = (error?: Error): void => {
      if (settled) {
        return;
      }
      settled = true;
      socket.destroy();
      if (error) {
        reject(error);
      } else {
        resolvePromise();
      }
    };
    socket.setTimeout(timeoutMs);
    socket.once("connect", () => finish());
    socket.once("timeout", () => finish(new Error(`${vscodeDownloadHost}:443 timed out`)));
    socket.once("error", (error) => finish(error));
    socket.connect(443, vscodeDownloadHost);
  });
}

async function main(): Promise<void> {
  const sourceExtensionPath = resolve(__dirname, "../../..");
  const extensionDevelopmentPath = process.env.ALYSIS_TEST_EXTENSION_PATH?.trim()
    ? resolve(process.env.ALYSIS_TEST_EXTENSION_PATH)
    : sourceExtensionPath;
  const sourceExtensionTestsPath = resolve(__dirname, "suite");
  const tempRoot = mkdtempSync(join(tmpdir(), "alysis-vscode-host-"));
  const workspacePath = join(tempRoot, "workspace");
  const userDataDir = join(tempRoot, "user-data");
  const extensionsDir = join(tempRoot, "extensions");
  const resultPath = join(tempRoot, "extension-host-result.json");
  try {
  mkdirSync(workspacePath, { recursive: true });
  mkdirSync(userDataDir, { recursive: true });
  mkdirSync(extensionsDir, { recursive: true });
  const packagePath = resolve(extensionDevelopmentPath, "package.json");
  if (!existsSync(packagePath)) {
    throw new Error(`Extension under test is missing package.json: ${extensionDevelopmentPath}`);
  }
  const packageManifest = JSON.parse(readFileSync(packagePath, "utf8")) as { main?: unknown };
  const extensionMain = typeof packageManifest.main === "string"
    ? resolve(extensionDevelopmentPath, packageManifest.main)
    : "";
  if (!extensionMain || !existsSync(extensionMain)) {
    throw new Error(`Extension under test is missing its declared main entry point: ${extensionMain || "(not declared)"}`);
  }
  // VS Code associates `require("vscode")` with the extension that owns the test
  // module. For VSIX dogfood, place the compiled suite inside the unpacked
  // extension so those imports resolve against the artifact under test instead
  // of the source tree.
  let extensionTestsPath = sourceExtensionTestsPath;
  if (extensionDevelopmentPath !== sourceExtensionPath) {
    const packagedTestsRoot = resolve(extensionDevelopmentPath, "out", "test", "integration");
    const packagedSuitePath = resolve(packagedTestsRoot, "suite");
    mkdirSync(packagedSuitePath, { recursive: true });
    cpSync(sourceExtensionTestsPath, packagedSuitePath, { recursive: true, force: true });
    // The suite's `../../../src/...` imports are test helpers (command ids, the trust evaluator,
    // git helpers), not the extension under test: VS Code loads that from the bundled
    // `dist/extension.js` the VSIX ships. The VSIX carries no `out/src` tree, so stage the
    // compiled helpers beside the staged tests, the same way mocha is staged below.
    cpSync(
      resolve(sourceExtensionPath, "out", "src"),
      resolve(extensionDevelopmentPath, "out", "src"),
      { recursive: true, force: true }
    );
    copyTestDependencyTree(
      "mocha",
      resolve(sourceExtensionPath, "node_modules"),
      resolve(packagedTestsRoot, "..", "node_modules")
    );
    extensionTestsPath = resolve(packagedTestsRoot, "run-packaged-tests.js");
    writeFileSync(
      extensionTestsPath,
      [
        '"use strict";',
        'module.exports = require("./suite");',
        ""
      ].join("\n"),
      { encoding: "utf8", mode: 0o600 }
    );
  }
  const mockCliPath = resolve(sourceExtensionPath, "test", "integration", "fixtures", "mock-alysis-cli.js");
  chmodSync(mockCliPath, 0o755);
  const workspaceSettingsDir = join(workspacePath, ".vscode");
  mkdirSync(workspaceSettingsDir, { recursive: true });
  writeFileSync(
    join(workspaceSettingsDir, "settings.json"),
    JSON.stringify(
      {
        "alysis.cliPath": mockCliPath,
        "alysis.autoStartBridge": true
      },
      null,
      2
    )
  );

  const vscodeExecutablePath = process.env.VSCODE_TEST_EXECUTABLE_PATH?.trim() || undefined;
  if (vscodeExecutablePath) {
    verifyExecutable(vscodeExecutablePath);
  } else {
    try {
      await preflightDownload();
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      throw new Error(
        `${extensionHostDownloadFailure} Set VSCODE_TEST_EXECUTABLE_PATH to a cached VS Code executable. Preflight: ${reason}`,
        { cause: error }
      );
    }
  }

  // Integrated terminals inherit this from VS Code's extension host. If it reaches
  // Code.exe, Electron treats the workspace path as a Node entry point instead of
  // launching the Extension Host test window.
  delete process.env.ELECTRON_RUN_AS_NODE;

    await runVsCodeExtensionHostTests({
      extensionDevelopmentPath,
      extensionTestsPath,
      vscodeExecutablePath,
      version: process.env.VSCODE_TEST_VERSION ?? "1.90.0",
      launchArgs: [
        workspacePath,
        "--user-data-dir",
        userDataDir,
        "--extensions-dir",
        extensionsDir,
        "--disable-extensions",
        "--skip-welcome",
        "--skip-release-notes"
      ],
      extensionTestsEnv: {
        ...process.env,
        ALYSIS_TEST_NODE_PATH: process.execPath,
        ALYSIS_TEST_RESULT_PATH: resultPath,
        ALYSIS_TEST_CLI_PATH: mockCliPath,
        ALYSIS_TEST_EXTENSION_PATH: extensionDevelopmentPath
      }
    });
    // Some editor forks exit zero even when the suite's rejected promise reports failures.
    // Require a fresh completion receipt from this isolated test profile as well as process success.
    if (!existsSync(resultPath)) throw new Error("Extension Host did not report a completed test suite.");
    assertSuccessfulExtensionHostResult(JSON.parse(readFileSync(resultPath, "utf8")));
  } finally {
    removeTempDirectory(tempRoot);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
