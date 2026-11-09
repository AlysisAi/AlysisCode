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
    windowApi.showInputBox = async () => "Build a Forge plan from Extension Host tests";
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
    assert.equal(evaluateWorkspaceTrust(false, "forgePlan").allowed, false);
    assert.equal(evaluateWorkspaceTrust(false, "forgeExecute").allowed, false);
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

  test("Forge Plan command updates view using mock bridge", async () => {
    await vscode.commands.executeCommand("alysis.forgePlan");

    const state = await waitForState(
      (candidate) =>
        candidate.forge.planId === "mock-plan" &&
        candidate.chat.cockpit.forge.artifacts.some((group: any) =>
          group.artifacts.some((artifact: any) => artifact.artifact_id === "forge_mock:plan/plan.json")
        )
    );
    const forgeState = state.forge;
    assert.ok(forgeState);
    assert.equal(forgeState.view.plan.plan_id, "mock-plan");
    assert.equal(forgeState.view.plan.tasks[0].task_id, "T01");
    assert.equal(forgeState.view.events.some((event: any) => event.type === "plan_node_updated"), true);
    assert.equal(state.chat.cockpit.forge.plan.plan_id, "mock-plan");
    assert.equal(state.chat.cockpit.forge.artifacts[0].artifacts[0].artifact_id, "forge_mock:plan/plan.json");
  });

  test("Forge diff open command works with mock diff payload", async () => {
    await vscode.commands.executeCommand("alysis.forgePlan");
    await waitForState((candidate) => candidate.forge.planId === "mock-plan");

    const commandsApi = vscode.commands as any;
    const originalExecute = commandsApi.executeCommand;
    const diffInvocations: any[] = [];
    commandsApi.executeCommand = async (command: string, ...args: any[]) => {
      if (command === "vscode.diff") {
        diffInvocations.push(args);
        return undefined;
      }
      return originalExecute.call(vscode.commands, command, ...args);
    };
    try {
      await vscode.commands.executeCommand("alysis.forge.openDiff", "mock-diff");

      const state = api.state();
      assert.ok(state.forge);
      assert.ok(state.chat);
      assert.equal(state.forge.view.diffs.some((diff: any) => diff.diff_id === "mock-diff"), true);
      assert.equal(state.chat.cockpit.forge.diffs.some((diff: any) => diff.diff_id === "mock-diff"), true);
      assert.equal(diffInvocations.length, 1);
      assert.equal(String(diffInvocations[0][0].scheme), "alysis-diff");
      assert.equal(String(diffInvocations[0][1].scheme), "alysis-diff");
    } finally {
      commandsApi.executeCommand = originalExecute;
    }
  });

  test("Forge artifact open command works with mock artifact payload", async () => {
    await vscode.commands.executeCommand("alysis.forgePlan");
    const state = await waitForState((candidate) =>
      candidate.chat.cockpit.forge.artifacts.some((group: any) =>
        group.artifacts.some((artifact: any) => artifact.artifact_id === "forge_mock:plan/plan.json")
      )
    );

    await vscode.commands.executeCommand(
      "alysis.artifact.open",
      state.chat.cockpit.forge.sessionId,
      "forge_mock:plan/plan.json"
    );

    assert.equal(state.chat.cockpit.forge.artifacts[0].artifacts[0].path, "plan/plan.json");
  });

  test("Forge Execute Preview command updates view with mock bridge", async () => {
    await vscode.commands.executeCommand("alysis.forgePlan");
    await waitForState((candidate) => candidate.forge.planId === "mock-plan");

    await vscode.commands.executeCommand("alysis.forgeExecute");

    const state = await waitForState((candidate) =>
      candidate.forge.view.events.some((event: any) =>
        String(event.description || "").includes("real execution is unavailable")
      )
    );
    assert.equal(state.forge.activeJobId, null);
    assert.equal(state.chat.cockpit.forge.executePreview.plan_id, "mock-plan");
    assert.equal(state.chat.cockpit.forge.executePreview.real_execution_supported, false);
  });

  test("typing /forge plan in chat starts Forge Plan with mock bridge", async () => {
    await api.submitChatText("/forge plan Build from slash command");

    const state = await waitForState(
      (candidate) =>
        candidate.forge.planId === "mock-plan" &&
        candidate.chat.cockpit.status.runtime.events.some((event: any) => event.title === "Slash command started")
    );
    assert.equal(state.forge.view.plan.plan_id, "mock-plan");
    assert.equal(state.chat.cockpit.forge.plan.plan_id, "mock-plan");
    assert.equal(state.chat.items.some((item: any) => String(item.text).includes("Planning this change.")), true);
    assert.equal(state.chat.cockpit.status.runtime.events.some((event: any) => event.title === "Forge planning started"), true);
    assert.equal(state.chat.cockpit.status.runtime.events.some((event: any) => event.title === "Forge Plan completed"), true);
  });

  test("typing /forge plan failure reports Timeline and Diagnostics errors", async () => {
    await api.submitChatText("/forge plan fail plan from cockpit e2e");

    const state = await waitForState((candidate) =>
      candidate.chat.cockpit.status.runtime.events.some((event: any) => event.title === "Forge Plan failed")
    );
    assert.equal(state.statusBar.state, "error");
    assert.equal(state.chat.cockpit.status.runtime.bridgeProcess.status, "error");
    assert.equal(
      state.chat.cockpit.status.runtime.events.some((event: any) =>
        event.title === "Forge Plan failed" &&
        String(event.message).includes("Mock Forge Plan failed")
      ),
      true
    );
  });

  test("typing /execute preview in chat updates Forge preview UI", async () => {
    await api.submitChatText("/forge plan Build preview target");
    await waitForState((candidate) => candidate.forge.planId === "mock-plan");

    await api.submitChatText("/execute preview");

    const state = await waitForState((candidate) =>
      candidate.forge.view.events.some((event: any) =>
        String(event.description || "").includes("real execution is unavailable")
      )
    );
    assert.equal(state.forge.activeJobId, null);
    assert.equal(state.chat.cockpit.forge.executePreview.selected_task_ids[0], "T01");
    assert.equal(state.chat.cockpit.forge.executePreview.next_recommended_action, "Review the preview.");
  });

  test("typing /execute plan runs deterministic review execution with verification and review events", async () => {
    const windowApi = vscode.window as any;
    const previousWarn = windowApi.showWarningMessage;
    windowApi.showWarningMessage = async (message: string) => {
      if (String(message).includes("Start Alysis Code Forge Execute")) {
        return "Start Execute";
      }
      return undefined;
    };
    try {
      await api.submitChatText("/forge plan execute review e2e workflow");
      await waitForState((candidate) => candidate.forge.planId === "mock-plan");

      await api.submitChatText("/execute plan");

      const state = await waitForState(
        (candidate) =>
          candidate.forge.activeJobId === null &&
          candidate.forge.view.events.some((event: any) => event.type === "verify_gate_result") &&
          candidate.forge.view.events.some((event: any) => event.type === "review_gate_decision") &&
          candidate.chat.cockpit.forge.diffs.some((diff: any) => diff.job_id === "mock-execute-job") &&
          candidate.chat.cockpit.forge.artifacts.some((group: any) =>
            group.artifacts.some((artifact: any) => artifact.artifact_id === "forge_mock:execute/result.json")
          )
      );
      assert.equal(state.chat.cockpit.forge.executePreview.real_execution_supported, true);
      assert.equal(state.forge.view.events.some((event: any) => event.label === "Verify passed"), true);
      assert.equal(state.forge.view.events.some((event: any) => event.label === "Review accepted"), true);
    } finally {
      windowApi.showWarningMessage = previousWarn;
    }
  });

  test("Forge async plan to execute review to diff keeps capabilities and failure dedup guarded", async () => {
    const initial = api.state();
    assert.ok(initial.chat);
    const initialChat = initial.chat;
    assert.equal(initialChat.cockpit.status.runtime.compatibility.features.forgePlan.supported, true);
    assert.equal(initialChat.cockpit.status.runtime.compatibility.features.forgeExecuteReview.supported, true);
    assert.equal(initialChat.cockpit.status.runtime.compatibility.features.diffs.supported, true);

    const windowApi = vscode.window as any;
    const previousWarn = windowApi.showWarningMessage;
    const commandsApi = vscode.commands as any;
    const originalExecute = commandsApi.executeCommand;
    const diffInvocations: any[] = [];
    windowApi.showWarningMessage = async (message: string) => {
      if (String(message).includes("Start Alysis Code Forge Execute")) {
        return "Start Execute";
      }
      return undefined;
    };
    commandsApi.executeCommand = async (command: string, ...args: any[]) => {
      if (command === "vscode.diff") {
        diffInvocations.push(args);
        return undefined;
      }
      return originalExecute.call(vscode.commands, command, ...args);
    };
    try {
      const beforePlan = api.state();
      assert.ok(beforePlan.chat);
      const beforePlanRuntimeIds = new Set(beforePlan.chat.cockpit.status.runtime.events.map((event: any) => event.id));
      await api.submitChatText("/forge plan execute review e2e async guard");
      const planned = await waitForState((candidate) =>
        candidate.forge.planId === "mock-plan" &&
        candidate.chat.cockpit.forge.plan.plan_id === "mock-plan" &&
        candidate.chat.cockpit.status.runtime.events.some(
          (event: any) => event.title === "Forge Plan completed" && !beforePlanRuntimeIds.has(event.id)
        )
      );
      assert.equal(
        planned.chat.cockpit.status.runtime.events.some((event: any) => String(event.message || "").includes("TypeError")),
        false
      );

      const beforeExecute = api.state();
      assert.ok(beforeExecute.forge);
      const beforeExecuteEventCount = beforeExecute.forge.view.events.length;
      await api.submitChatText("/execute plan");
      const executed = await waitForState(
        (candidate) =>
          candidate.forge.view.events.length > beforeExecuteEventCount &&
          candidate.forge.activeJobId === null &&
          candidate.forge.view.events.some((event: any) => event.type === "verify_gate_result") &&
          candidate.forge.view.events.some((event: any) => event.type === "review_gate_decision") &&
          candidate.chat.cockpit.forge.executePreview?.real_execution_supported === true &&
          candidate.chat.cockpit.forge.diffs.some((diff: any) => diff.diff_id === "mock-diff")
      );
      assert.equal(executed.chat.cockpit.forge.executePreview.real_execution_supported, true);

      await vscode.commands.executeCommand("alysis.forge.openDiff", "mock-diff");
      assert.equal(diffInvocations.length, 1);
      assert.equal(String(diffInvocations[0][0].scheme), "alysis-diff");
      assert.equal(String(diffInvocations[0][1].scheme), "alysis-diff");

      const beforeFailure = api.state();
      assert.ok(beforeFailure.chat);
      const beforeRuntimeIds = new Set(beforeFailure.chat.cockpit.status.runtime.events.map((event: any) => event.id));
      await api.submitChatText("/forge plan fail plan from async guard");

      const failed = await waitForState((candidate) =>
        candidate.chat.cockpit.status.runtime.events.some(
          (event: any) => event.title === "Forge Plan failed" && !beforeRuntimeIds.has(event.id)
        )
      );
      const newRuntimeEvents = failed.chat.cockpit.status.runtime.events.filter((event: any) => !beforeRuntimeIds.has(event.id));
      const runtimeFailures = newRuntimeEvents.filter((event: any) => event.title === "Forge Plan failed");
      assert.equal(runtimeFailures.length, 1);
      assert.match(String(runtimeFailures[0].message), /Mock Forge Plan failed/);
      assert.ok(runtimeFailures[0].rootCauseKey);
      assert.equal(newRuntimeEvents.some((event: any) => event.title === "Forge error"), false);
      assert.equal(
        failed.chat.cockpit.forge.events.some(
          (event: any) => event.label === "forge_ui_error" && event.rootCauseKey === runtimeFailures[0].rootCauseKey
        ),
        false
      );
      const forgeErrors = failed.chat.cockpit.forge.events.filter(
        (event: any) => event.severity === "error" && event.rootCauseKey === runtimeFailures[0].rootCauseKey
      );
      assert.equal(forgeErrors.length <= 1, true);
      if (forgeErrors.length === 1) {
        assert.equal(forgeErrors[0].rootCauseKey, runtimeFailures[0].rootCauseKey);
      }
    } finally {
      windowApi.showWarningMessage = previousWarn;
      commandsApi.executeCommand = originalExecute;
    }
  });

  test("cockpit exposes Forge approval cards and denial responds through mock bridge", async () => {
    const windowApi = vscode.window as any;
    const previousWarn = windowApi.showWarningMessage;
    let releaseApprovalModal: (value: unknown) => void = () => undefined;
    windowApi.showWarningMessage = async (message: string) => {
      if (String(message).includes("Alysis Code Forge approval required")) {
        return new Promise((resolve) => {
          releaseApprovalModal = resolve;
        });
      }
      return undefined;
    };
    try {
      await api.submitChatText("/forge plan Approval cockpit target");
      await waitForState((candidate) => candidate.forge.planId === "mock-plan");

      await api.submitChatText("/execute preview");

      const pending = await waitForState((candidate) =>
        candidate.chat.cockpit.forge.approvals.some((approval: any) => approval.approvalId === "mock-approval")
      );
      const approval = pending.chat.cockpit.forge.approvals.find((candidate: any) => candidate.approvalId === "mock-approval");
      assert.equal(approval.kind, "verify_run");
      assert.equal(approval.command, "npm test");
      assert.equal(approval.allowForSessionSupported, true);

      await api.respondForgeApproval(approval.sessionId, "mock-approval", "deny");

      const denied = await waitForState(
        (candidate) =>
          candidate.chat.cockpit.forge.approvals.length === 0 &&
          candidate.chat.items.some((item: any) =>
            item.kind === "approval" && String(item.text || "").includes("Decision: denied")
          )
      );
      assert.equal(denied.chat.cockpit.forge.approvals.length, 0);
      assert.equal(denied.chat.cockpit.status.runtime.bridgeProcess.message, "Forge approval was answered from cockpit.");
    } finally {
      releaseApprovalModal(undefined);
      windowApi.showWarningMessage = previousWarn;
    }
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
    await api.submitChatText("/forge plan trigger bridge exit from cockpit e2e");

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

  test("Run Swarm drives a full parallel swarm: approval, scope-violation failure, review, keep/discard", async () => {
    // 1. Plan establishes the session/forge plan the swarm runs against.
    await vscode.commands.executeCommand("alysis.forgePlan");
    await waitForState((candidate) => candidate.forge.planId === "mock-plan");

    // The Run Swarm capability is live (the mock advertises the swarm methods).
    let state: any = api.state();
    assert.ok(state.forge, "forge state present");
    assert.equal(state.forge.swarm.supported, true, "Run Swarm must be capability-supported with the mock swarm methods");

    // 2. Start the swarm. The mock emits a deterministic 3-task lifecycle:
    //    T01 pauses on an approval, T02 fails closed on a scope violation,
    //    T03 serializes and completes.
    await vscode.commands.executeCommand("alysis.runSwarm");

    // 3. The task grid shows the parallel workers by render state, including
    //    the one failed task (one failure → one error-card-equivalent state).
    state = await waitForState((candidate) => {
      const byId: Record<string, string> = {};
      for (const task of candidate.forge.swarm.tasks || []) {
        byId[task.taskId] = task.state;
      }
      return byId.T02 === "failed" && (byId.T01 === "approval" || byId.T01 === "running");
    });
    const failedCount = (state.forge.swarm.tasks || []).filter((t: any) => t.state === "failed").length;
    assert.equal(failedCount, 1, "exactly one task fails on the scope violation");

    // 4. The approval is non-modal and task-attributed (T01 / worker label).
    state = await waitForState((candidate) =>
      (candidate.chat.cockpit.forge.approvals || []).some((a: any) => a.swarmTaskId === "T01")
    );
    assert.ok(state.chat, "chat state present");
    const approval = state.chat.cockpit.forge.approvals.find((a: any) => a.swarmTaskId === "T01");
    assert.ok(approval, "swarm approval must carry task attribution");
    assert.equal(approval.swarmWorker, "forge_swarm:T01");

    // 5. Approve it — the worker resumes.
    await api.respondForgeApproval(approval.sessionId, approval.approvalId, "allow_once");

    // 6. After the run finishes, the per-task review lists pending Keep/Discard
    //    items with the untracked-files note surfaced and a recovery offer for
    //    the failed task. The working tree is untouched until apply.
    state = await waitForState((candidate) => (candidate.forge.swarm.pendingReviewTaskIds || []).length >= 1);
    assert.ok(state.forge, "forge state present after review");
    const swarm = state.forge.swarm;
    assert.equal(swarm.workingTreeUntouchedUntilApply, true);
    assert.deepEqual([...swarm.pendingReviewTaskIds].sort(), ["T01", "T03"]);
    const t03 = swarm.tasks.find((t: any) => t.taskId === "T03");
    assert.ok(t03 && t03.reviewable);
    assert.deepEqual(t03.untrackedFilesNotApplied, ["scratch_note.txt"]);
    const t02 = swarm.tasks.find((t: any) => t.taskId === "T02");
    assert.ok(t02 && t02.recovery && t02.recovery.instruction, "failed task offers an editable regenerate instruction");
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
