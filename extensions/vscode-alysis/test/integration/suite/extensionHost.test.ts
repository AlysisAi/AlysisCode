import * as assert from "node:assert/strict";
import { realpathSync } from "node:fs";

import * as vscode from "vscode";

import { AlysisExtensionApi } from "../../../src/extension";
import { COMMANDS } from "../../../src/commands/registry";
import { evaluateWorkspaceTrust } from "../../../src/security/workspaceTrust";
import { runGit, inspectGitWorkspace } from "../../../src/workspace/GitWorktrees";

let api: NonNullable<AlysisExtensionApi["test"]>;

suite("Alysis Code Extension Host", () => {
  let extension: vscode.Extension<AlysisExtensionApi | undefined>;
  let originalInfo: typeof vscode.window.showInformationMessage;
  let originalWarn: typeof vscode.window.showWarningMessage;
  let originalError: typeof vscode.window.showErrorMessage;
  let originalInput: typeof vscode.window.showInputBox;
  let originalQuickPick: typeof vscode.window.showQuickPick;
  let originalShowTextDocument: typeof vscode.window.showTextDocument;

  suiteSetup(async () => {
    const mockCliPath = process.env.ALYSIS_TEST_CLI_PATH;
    assert.ok(mockCliPath, "ALYSIS_TEST_CLI_PATH should point at the mock CLI fixture.");
    await vscode.workspace
      .getConfiguration("alysis")
      .update("cliPath", mockCliPath, vscode.ConfigurationTarget.Global);
    await vscode.workspace
      .getConfiguration("alysis")
      .update("autoStartBridge", true, vscode.ConfigurationTarget.Global);
    originalInfo = vscode.window.showInformationMessage;
    originalWarn = vscode.window.showWarningMessage;
    originalError = vscode.window.showErrorMessage;
    originalInput = vscode.window.showInputBox;
    originalQuickPick = vscode.window.showQuickPick;
    originalShowTextDocument = vscode.window.showTextDocument;
    const windowApi = vscode.window as any;
    windowApi.showInformationMessage = async () => undefined;
    windowApi.showWarningMessage = async () => undefined;
    windowApi.showErrorMessage = async () => undefined;
    windowApi.showInputBox = async () => "Extension Host test input";
    windowApi.showQuickPick = async (items: any, options?: { canPickMany?: boolean }) => {
      const resolved = await Promise.resolve(items);
      return options?.canPickMany ? [resolved[0]] : resolved[0];
    };
    windowApi.showTextDocument = async () => undefined;
    extension = vscode.extensions.getExtension("alysisai.vscode-alysis") as vscode.Extension<AlysisExtensionApi | undefined>;
    assert.ok(extension, "Alysis Code extension should be discoverable by publisher/name.");
    const expectedExtensionPath = process.env.ALYSIS_TEST_EXTENSION_PATH;
    assert.ok(expectedExtensionPath, "ALYSIS_TEST_EXTENSION_PATH should identify the artifact under test.");
    assert.equal(
      realpathSync(extension.extensionPath),
      realpathSync(expectedExtensionPath),
      "VS Code must activate the exact source tree or unpacked VSIX selected by the dogfood runner."
    );
    const exports = await extension.activate();
    assert.ok(exports?.test, "Alysis Code extension should expose test API only in Extension Host test mode.");
    api = exports.test;
    // Runtime validation and the first health probe are deliberately off the activation critical path.
    await api.whenReady();
  });

  suiteTeardown(async () => {
    const windowApi = vscode.window as any;
    windowApi.showInformationMessage = originalInfo;
    windowApi.showWarningMessage = originalWarn;
    windowApi.showErrorMessage = originalError;
    windowApi.showInputBox = originalInput;
    windowApi.showQuickPick = originalQuickPick;
    windowApi.showTextDocument = originalShowTextDocument;
    await vscode.workspace
      .getConfiguration("alysis")
      .update("cliPath", undefined, vscode.ConfigurationTarget.Global);
    await vscode.workspace
      .getConfiguration("alysis")
      .update("autoStartBridge", undefined, vscode.ConfigurationTarget.Global);
  });

  test("activation health handshake populates Manage capabilities without Show Bridge Health", async () => {
    assert.equal(extension.isActive, true);

    const state = api.state();
    assert.ok(state.statusBar, "status bar state should be available.");
    assert.equal(state.statusBar.state, "bridgeOk");
    assert.match(state.statusBar.text, /Alysis Code/);
    assert.equal(state.statusBar.visible, true);
    assert.equal(state.chat?.cockpit.status.runtime.compatibility.features.tools.supported, true);
    assert.equal(state.chat?.cockpit.status.runtime.compatibility.features.skills.supported, true);
    assert.equal(state.chat?.cockpit.status.runtime.compatibility.features.mcp.supported, true);
    assert.equal(state.chat?.cockpit.status.runtime.compatibility.features.hooks.supported, true);
    assert.equal(state.chat?.cockpit.status.runtime.compatibility.features.conventions.supported, true);
    assert.equal(state.chat?.cockpit.status.runtime.compatibility.features.ext.supported, true);
    assert.equal(state.chat?.cockpit.status.runtime.compatibility.features.managedBrowser.supported, true);
    assert.equal(state.chat?.cockpit.status.runtime.compatibility.features.managedBrowserDirectLoopback.supported, true);
  });

  test("registers contributed commands", async () => {
    const registered = new Set(await vscode.commands.getCommands(true));
    for (const command of Object.values(COMMANDS)) {
      assert.equal(registered.has(command), true, `${command} should be registered`);
    }
  });

  test("contributes Activity Bar container and views", async () => {
    const packageJson = extension.packageJSON as {
      contributes: {
        viewsContainers: { activitybar: Array<{ id: string; icon: string }> };
        views: Record<string, Array<{ id: string; name: string }>>;
      };
    };
    assert.ok(packageJson.contributes.viewsContainers.activitybar.some((view) => view.id === "alysis"));
    assert.deepEqual(
      packageJson.contributes.views.alysis.map((view) => view.id).sort(),
      ["alysis.start"]
    );

    await vscode.commands.executeCommand("workbench.view.extension.alysis");
  });

  test("exercises SecretStorage wrapper safely", async () => {
    const result = await api.secretStorageRoundTrip();

    assert.deepEqual(result, { stored: true, cleared: true });
  });

  test("keeps Workspace Trust gates fail-closed for unsafe scaffold commands", () => {
    assert.equal(evaluateWorkspaceTrust(false, "mutatingAction").allowed, false);
    assert.equal(evaluateWorkspaceTrust(false, "readonlyPlaceholder").allowed, true);
  });

  test("opens the primary sidebar as the only chat surface", async () => {
    await vscode.commands.executeCommand("alysis.openChat");

    const state = api.state();
    assert.ok(state.chat, "chat state should be available after openChat");
    assert.equal(typeof state.chat?.readiness.ok, "boolean");
  });

  test("Managed Browser completes public and direct-loopback verified lifecycles", async () => {
    await vscode.commands.executeCommand("alysis.showBrowser");
    // Opening the sidebar starts background catalog requests. The first owner
    // session upgrades the credential-free bridge and must not interrupt them.
    await waitForState((candidate) => candidate.bridgeCanRestart);
    await api.browserStart();

    let browser = api.state().browser;
    assert.ok(browser, "managed browser state should be available");
    assert.equal(browser.supported, true);
    assert.equal(browser.phase, "ready");
    assert.equal(browser.sessions.length, 1);
    assert.equal(browser.sessions[0].browserSessionId, "mock-browser-session-0001");
    assert.equal(browser.sessions[0].networkScope, "public");
    assert.equal(browser.sessions[0].activeUrl, null);

    await api.browserNavigate("https://example.test/public/path?secret=not-rendered#fragment");
    browser = api.state().browser;
    assert.equal(browser?.sessions[0].activeUrl, "https://example.test/public/path");

    await api.browserSnapshot("text");
    browser = api.state().browser;
    assert.equal(browser?.snapshot?.kind, "text");
    assert.match(browser?.snapshot?.preview ?? "", /Managed Browser extension-host fixture/);
    assert.equal(browser?.snapshot?.truncated, false);

    await api.browserScreenshot(false);
    browser = api.state().browser;
    assert.equal(browser?.screenshot?.artifactId, "browser:mock-browser-session-0001:screenshot-0001-extension-host.png");
    assert.ok((browser?.screenshot?.sizeBytes ?? 0) > 1024 * 1024, "fixture should exercise chunked artifact reads");
    assert.match(browser?.screenshot?.sha256 ?? "", /^[a-f0-9]{64}$/);
    assert.match(browser?.screenshot?.previewKey ?? "", /^[a-f0-9]{64}\.png$/);
    assert.equal(browser?.sessions[0].artifactCount, 1);

    await api.browserDiagnostics();
    browser = api.state().browser;
    assert.deepEqual(browser?.diagnostics.map((event) => event.category), ["console", "network"]);
    assert.equal(browser?.diagnosticsTruncated, false);

    const windowApi = vscode.window as any;
    const previousWarn = windowApi.showWarningMessage;
    windowApi.showWarningMessage = async () => "Close and delete";
    try {
      await api.browserClose();
    } finally {
      windowApi.showWarningMessage = previousWarn;
    }

    browser = api.state().browser;
    assert.equal(browser?.phase, "idle");
    assert.deepEqual(browser?.sessions, []);
    assert.equal(browser?.selectedBrowserId, null);
    assert.equal(browser?.snapshot, null);
    assert.equal(browser?.screenshot, null);
    assert.deepEqual(browser?.diagnostics, []);
    assert.match(browser?.notice ?? "", /ephemeral screenshots were deleted/);

    await api.browserStartLocal();
    browser = api.state().browser;
    assert.equal(browser?.localTestingSupported, true);
    assert.equal(browser?.phase, "ready");
    assert.equal(browser?.sessions.length, 1);
    assert.equal(browser?.sessions[0].browserSessionId, "mock-loopback-session-0001");
    assert.equal(browser?.sessions[0].networkScope, "public_loopback");
    assert.match(browser?.notice ?? "", /direct IDE use.*agent cannot access/i);

    await api.browserNavigate("http://127.0.0.1:4173/health?token=not-rendered#fragment");
    browser = api.state().browser;
    assert.equal(browser?.sessions[0].activeUrl, "http://127.0.0.1:4173/health");

    for (const deniedTarget of ["http://192.168.1.10/admin", "http://169.254.169.254/latest/meta-data/"]) {
      await api.browserNavigate(deniedTarget);
      browser = api.state().browser;
      assert.equal(browser?.phase, "error");
      assert.ok(browser?.error, `${deniedTarget} should fail closed`);
      assert.equal(browser?.sessions[0].activeUrl, "http://127.0.0.1:4173/health");
    }

    await api.browserClose();
    browser = api.state().browser;
    assert.equal(browser?.phase, "idle");
    assert.deepEqual(browser?.sessions, []);
    assert.equal(browser?.selectedBrowserId, null);
  });

  test("typing /help and unknown slash commands stay inside chat", async () => {
    await api.submitChatText("/help");
    await api.submitChatText("/definitely-not-a-command");

    const state = api.state();
    assert.ok(state.chat);
    assert.equal(state.chat.items.some((item: any) => String(item.text).includes("Alysis Code slash commands")), true);
    assert.equal(state.chat.items.some((item: any) => String(item.text).includes("Unknown slash command")), true);
    assert.equal(state.chat.activeJobId, null);
  });

  test("/doctor failure reaches Cockpit Timeline and Diagnostics with mock CLI", async () => {
    await withMockScenario("doctor-failure", async () => {
      await api.submitChatText("/doctor");

      const state = await waitForState((candidate) =>
        candidate.chat.cockpit.status.runtime.events.some((event: any) => event.title === "Doctor failed" && event.exitCode === 2)
      );
      const failure = state.chat.cockpit.status.runtime.events.find((event: any) => event.title === "Doctor failed" && event.exitCode === 2);
      assert.equal(state.statusBar.state, "error");
      assert.equal(state.chat.cockpit.status.runtime.sandboxDoctor.status, "failed");
      // The sandbox doctor failed, but the already-handshaken live bridge remains the authoritative
      // CLI-health signal. Keep that connection healthy while surfacing the doctor failure separately.
      assert.equal(state.chat.cockpit.status.runtime.cliHealth.status, "ok");
      assert.ok(failure);
      assert.doesNotMatch(`${failure.stdout}\n${failure.stderr}`, /sk-test-secret-value|abcdefghijklmnop/);
      assert.match(`${failure.stdout}\n${failure.stderr}`, /<redacted>/);
    });
  });

  test("an out-of-band degraded health probe does not re-gate a healthy live bridge", async () => {
    await withMockScenario("health-missing-methods", async () => {
      await vscode.commands.executeCommand("alysis.showBridgeHealth");

      const state = await waitForState(
        (candidate) =>
          candidate.statusBar.state === "bridgeOk" &&
          candidate.chat.cockpit.status.runtime.cliHealth.status === "ok" &&
          candidate.chat.cockpit.status.runtime.bridgeProtocol.status === "ok" &&
          candidate.chat.cockpit.status.runtime.bridgeProcess.status === "ready" &&
          candidate.chat.cockpit.status.runtime.compatibility.features.artifacts.supported === true
      );
      assert.equal(state.chat.cockpit.status.runtime.cliOrigin.status, "trusted");
      assert.equal(
        state.chat.cockpit.status.runtime.events.some((event: any) => event.title === "Bridge health ok"),
        true
      );
    });
  });

  test("normal scaffold commands do not throw with mock CLI fixture", async () => {
    await vscode.commands.executeCommand("alysis.openChat");
    await vscode.commands.executeCommand("alysis.showBridgeHealth");
    await vscode.commands.executeCommand("alysis.runDoctor");
    await vscode.commands.executeCommand("alysis.cancelCurrentRun");
  });

  test("capability-negotiated Tasks host action completes through the real Extension Host", async () => {
    // The actual composer waits for provider readiness. Load it explicitly instead of depending
    // on the editor's webview startup timing before bypassing the composer via this test API.
    await vscode.commands.executeCommand("alysis.showModels");
    await waitForState((candidate) => candidate.chat.readiness.ok);
    const before = api.hostActions().length;
    const verificationBefore = api.state().chat?.cockpit.status.runtime.events.filter(
      (event: any) => event.source === "verification"
    ).length ?? 0;
    const started = await api.startTask("Exercise the negotiated Extension Host task catalog", "review");
    const chat = api.state().chat;
    assert.equal(
      started,
      true,
      "the real run.start path should accept the host-action exercise: " + JSON.stringify({
        activeJobId: chat?.activeJobId,
        readiness: chat?.readiness,
        recentItems: chat?.items.slice(-4)
      })
    );
    const entries = await waitForHostActions((candidate) => candidate.length > before);
    const completed = entries.slice(before).find((entry) => entry.action === "tasks.list");
    assert.ok(completed, "the mock bridge should request the negotiated Tasks catalog");
    assert.equal(completed.outcome, "result");
    assert.match(completed.hostActionId, /^ha_[a-f0-9]{32}$/);
    await waitForState((candidate) =>
      candidate.chat.activeJobId === null
      && candidate.chat.cockpit.status.runtime.events.filter(
        (event: any) => event.source === "verification"
      ).length > verificationBefore
    );
  });

  test("an attached VS Code diagnostic crosses the bridge lifecycle and is observed cleared", async () => {
    await vscode.commands.executeCommand("alysis.showModels");
    const folder = vscode.workspace.workspaceFolders?.[0];
    assert.ok(folder, "diagnostic lifecycle requires the disposable workspace");
    const uri = vscode.Uri.joinPath(folder.uri, "diagnostic-gate.ts");
    const collection = vscode.languages.createDiagnosticCollection("alysis-extension-host-verification");
    const diagnostic = new vscode.Diagnostic(
      new vscode.Range(0, 0, 0, 5),
      "Extension Host attached diagnostic must clear",
      vscode.DiagnosticSeverity.Error
    );
    diagnostic.source = "alysis-extension-host-test";
    diagnostic.code = "attached-clear";
    await vscode.workspace.fs.writeFile(uri, Buffer.from("export const alysisDiagnosticGateValue = 1;\n", "utf8"));
    await vscode.workspace.openTextDocument(uri);
    collection.set(uri, [diagnostic]);
    try {
      await waitForDiagnosticsToSettle(uri);
      assert.equal(
        vscode.languages.getDiagnostics(uri).some((entry) => entry.code === "attached-clear"),
        true,
        "the real VS Code diagnostic collection must expose the attached diagnostic"
      );
      assert.deepEqual(
        vscode.languages.getDiagnostics(uri).filter((entry) => entry.source !== "alysis-extension-host-test"),
        [],
        "the fixture document must be language-service clean before the diagnostic baseline is captured"
      );
      const verificationCount = api.state().chat?.cockpit.status.runtime.events.filter(
        (event: any) => event.source === "verification"
      ).length ?? 0;
      await api.submitChatText("Fix the attached Extension Host diagnostic");
      collection.delete(uri);
      await waitForDiagnosticsToSettle(uri);
      assert.equal(
        vscode.languages.getDiagnostics(uri).some((entry) => entry.code === "attached-clear"),
        false,
        "the attached VS Code diagnostic must be absent before the mock run completes"
      );
      const state = await waitForState((candidate) =>
        candidate.chat.cockpit.status.runtime.events.filter(
          (event: any) => event.source === "verification"
        ).length > verificationCount
      );
      const event = state.chat.cockpit.status.runtime.events.find((candidate: any) =>
        candidate.source === "verification"
      );
      const currentDiagnostics = vscode.languages.getDiagnostics().flatMap(([diagnosticUri, entries]) =>
        entries.map((entry) => ({
          uri: diagnosticUri.toString(),
          message: entry.message,
          source: entry.source,
          code: typeof entry.code === "object" ? entry.code.value : entry.code
        }))
      );
      assert.equal(
        event?.title,
        "Diagnostics verification passed",
        `${String(event?.message)}; current=${JSON.stringify(currentDiagnostics)}`
      );
      assert.match(String(event?.message), /attached diagnostic was cleared/i);
      assert.match(String(event?.details), /1\/1 attached diagnostic\(s\) cleared/);
    } finally {
      collection.clear();
      collection.dispose();
      await vscode.workspace.fs.delete(uri, { useTrash: false });
    }
  });

  test("mock bridge exit reports a Cockpit runtime event without hanging", async () => {
    await api.submitChatText("trigger bridge exit from cockpit e2e");

    const state = await waitForState((candidate) =>
      candidate.chat.cockpit.status.runtime.events.some((event: any) => event.title === "Bridge exited")
    );
    assert.equal(
      state.chat.cockpit.status.runtime.events.some((event: any) =>
        event.title === "Bridge exited" &&
        String(event.message).includes("exited with code 7")
      ),
      true
    );
  });

  test("New Worktree command creates a real Git branch and opens a separate window without replacing the source workspace", async function () {
    this.timeout(60_000);
    await vscode.commands.executeCommand(COMMANDS.newSession);
    const root = vscode.workspace.workspaceFolders![0].uri.fsPath;
    await runGit(root, ["init", "-b", "main"]);
    await runGit(root, ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "initial"]);
    const input = vscode.window.showInputBox;
    const errors: string[] = [];
    const showError = vscode.window.showErrorMessage;
    const execute = vscode.commands.executeCommand;
    let opened: { uri: vscode.Uri; options: { forceNewWindow: boolean } } | undefined;
    (vscode.commands as any).executeCommand = async (command: string, ...args: any[]) => {
      if (command === "vscode.openFolder") { opened = { uri: args[0], options: args[1] }; return; }
      return execute(command, ...args);
    };
    (vscode.window as any).showInputBox = async () => "alysis/host-worktree";
    (vscode.window as any).showErrorMessage = async (message: string) => { errors.push(message); };
    try {
      await vscode.commands.executeCommand(COMMANDS.newWorktree);
      assert.deepEqual(errors, []);
      assert.ok(opened);
      assert.equal(opened.options.forceNewWindow, true);
      const worktreeRoot = opened.uri.fsPath;
      assert.notEqual(worktreeRoot, root);
      assert.ok(worktreeRoot.includes("-alysis-"));
      assert.equal(vscode.workspace.workspaceFolders![0].uri.fsPath, root);
      const repo = await inspectGitWorkspace(worktreeRoot);
      assert.equal(repo.branch, "alysis/host-worktree");
      assert.equal(repo.isWorktree, true);
    } finally {
      (vscode.window as any).showInputBox = input;
      (vscode.window as any).showErrorMessage = showError;
      (vscode.commands as any).executeCommand = execute;
    }
  });
});

async function waitForState(predicate: (state: any) => boolean): Promise<any> {
  const deadline = Date.now() + 8000;
  let latest: any;
  while (Date.now() < deadline) {
    latest = api.state();
    if (predicate(latest)) {
      return latest;
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  assert.fail(`timed out waiting for extension state; latest=${JSON.stringify(latest)}`);
}

async function waitForHostActions(
  predicate: (entries: ReturnType<typeof api.hostActions>) => boolean
): Promise<ReturnType<typeof api.hostActions>> {
  const deadline = Date.now() + 8000;
  let latest = api.hostActions();
  while (Date.now() < deadline) {
    latest = api.hostActions();
    if (predicate(latest)) {
      return latest;
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  assert.fail(`timed out waiting for host action completion; latest=${JSON.stringify(latest)}`);
}

async function waitForDiagnosticsToSettle(
  uri: vscode.Uri,
  quietMs = 250,
  timeoutMs = 3000
): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    let settled = false;
    let quietTimer: NodeJS.Timeout | undefined;
    let subscription: vscode.Disposable | undefined;
    const deadline = setTimeout(() => finish(new Error(`diagnostics did not settle for ${uri.toString()}`)), timeoutMs);
    const finish = (error?: Error): void => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(deadline);
      if (quietTimer) {
        clearTimeout(quietTimer);
      }
      subscription?.dispose();
      if (error) {
        reject(error);
      } else {
        resolve();
      }
    };
    const arm = (): void => {
      if (quietTimer) {
        clearTimeout(quietTimer);
      }
      quietTimer = setTimeout(() => finish(), quietMs);
    };
    subscription = vscode.languages.onDidChangeDiagnostics((event) => {
      if (event.uris.some((changed) => changed.toString() === uri.toString())) {
        arm();
      }
    });
    arm();
  });
}

async function withMockScenario<T>(scenario: string, callback: () => Promise<T>): Promise<T> {
  const previous = process.env.ALYSIS_TEST_MOCK_SCENARIO;
  process.env.ALYSIS_TEST_MOCK_SCENARIO = scenario;
  try {
    return await callback();
  } finally {
    if (previous === undefined) {
      delete process.env.ALYSIS_TEST_MOCK_SCENARIO;
    } else {
      process.env.ALYSIS_TEST_MOCK_SCENARIO = previous;
    }
  }
}
