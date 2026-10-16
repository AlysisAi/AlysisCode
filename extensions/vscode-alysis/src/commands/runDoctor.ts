import * as vscode from "vscode";

import { BridgeHealthError } from "../client/AlysisProtocol";
import {
  CliCommandResult,
  CliDiscovery,
  AlysisConfig,
  looksLikeMissingSubcommand,
  redactForDisplay,
  resolveCliExecutionPath
} from "../client/CliDiscovery";
import { CLI_UPGRADE_COMMAND } from "../client/compatibility";
import { classifyChatError } from "../chat/ChatErrorTaxonomy";
import { CockpitRuntimeState, cliHealthFromFailureCode, protocolStatusFromCode } from "../chat/CockpitRuntimeState";
import { evaluateProcessExecutionCommand } from "../security/commandGuards";
import { AlysisStatusBar } from "../status/statusBar";

export function registerRunDoctorCommand(
  context: vscode.ExtensionContext,
  cliDiscovery: CliDiscovery,
  getConfig: () => AlysisConfig,
  output: vscode.OutputChannel,
  statusBar: AlysisStatusBar,
  runtime?: CockpitRuntimeState,
  ensureRuntimeReady?: () => Promise<void>
): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("alysis.runDoctor", async () => {
      await ensureRuntimeReady?.();
      const config = getConfig();
      runtime?.applyConfig(config);
      const guard = evaluateProcessExecutionCommand(config, "runDoctor");
      const commandLabel = "alysis sandbox doctor --smoke";
      runtime?.setSandboxDoctor("running", `Running ${commandLabel}.`);
      runtime?.recordEvent({
        severity: "info",
        source: "doctor",
        title: "Doctor started",
        message: commandLabel,
        command: commandLabel
      });
      if (!guard.allowed) {
        const message = guard.reason ?? "Alysis Code CLI execution is blocked by Workspace Trust.";
        runtime?.setCliOrigin("blocked", message);
        runtime?.setCliHealth("unknown", "CLI health was not checked because origin is blocked.");
        runtime?.setBridgeProcess("error", message);
        runtime?.setSandboxDoctor("failed", message);
        runtime?.recordEvent({
          severity: "error",
          source: "doctor",
          title: "Doctor failed",
          message,
          command: commandLabel,
          details: "The Alysis Code CLI path is blocked before execution."
        });
        statusBar.setError(message);
        output.clear();
        output.appendLine(message);
        output.show(true);
        await vscode.window.showWarningMessage(message);
        return;
      }
      const cliPath = resolveCliExecutionPath(config);
      output.clear();
      output.appendLine(`Running: ${cliPath} sandbox doctor --smoke`);
      let result: CliCommandResult;
      try {
        result = await withCommandProgress(
          "Alysis Code is running sandbox doctor",
          () => cliDiscovery.runDoctor(cliPath, config)
        );
      } catch (error) {
        const message = commandErrorMessage(error);
        runtime?.setSandboxDoctor("failed", message);
        runtime?.setCliHealth(errorCode(error) === "ENOENT" ? "unreachable" : "broken", message);
        runtime?.recordEvent({
          severity: "error",
          source: "doctor",
          title: "Doctor failed",
          message,
          command: commandLabel,
          details: "Open the Alysis Code Output panel for full output."
        });
        statusBar.setError("Alysis Code sandbox doctor failed");
        output.appendLine(message);
        output.show(true);
        await vscode.window.showErrorMessage(message);
        return;
      }
      output.appendLine(redactForDisplay(result.stdout));
      if (result.stderr.trim().length > 0) {
        output.appendLine(redactForDisplay(result.stderr));
      }
      output.show(true);

      if (result.exitCode === 0) {
        runtime?.setCliHealth("ok", "Alysis Code CLI responded to sandbox doctor.");
        runtime?.setSandboxDoctor("ok", "Alysis Code sandbox doctor completed.");
        runtime?.recordEvent({
          severity: "info",
          source: "doctor",
          title: "Doctor completed",
          message: "Alysis Code sandbox doctor completed.",
          command: commandLabel,
          exitCode: result.exitCode,
          stdout: preview(result.stdout),
          stderr: preview(result.stderr),
          details: "Open the Alysis Code Output panel for full output."
        });
        const currentStatus = statusBar.snapshot();
        if (currentStatus.state === "error" && currentStatus.tooltip.includes("sandbox doctor")) {
          statusBar.setIdle();
        }
        await vscode.window.showInformationMessage("Alysis Code sandbox doctor completed.");
      } else {
        const doctorOutput = `${result.stdout}\n${result.stderr}`;
        const missingDoctor =
          looksLikeMissingSubcommand(doctorOutput, "sandbox") ||
          looksLikeMissingSubcommand(doctorOutput, "doctor");
        const failure = classifyChatError(doctorOutput);
        const runnerUnavailable = !missingDoctor && failure.kind === "sandbox_unavailable";
        const message = missingDoctor
          ? `The installed Alysis Code CLI does not support \`alysis sandbox doctor --smoke\`. Upgrade with: ${CLI_UPGRADE_COMMAND}`
          : runnerUnavailable ? failure.detail : `Alysis Code sandbox doctor failed with exit code ${result.exitCode}.`;
        runtime?.setCliHealth(runnerUnavailable ? "ok" : "broken", runnerUnavailable
          ? "Alysis Code responded; command execution needs setup."
          : missingDoctor ? "Alysis Code CLI is incomplete or too old for sandbox doctor." : `Sandbox doctor exited with code ${result.exitCode}.`);
        runtime?.setSandboxDoctor("failed", message);
        runtime?.recordEvent({
          severity: "error",
          source: "doctor",
          title: "Doctor failed",
          message,
          command: commandLabel,
          exitCode: result.exitCode,
          stdout: preview(result.stdout),
          stderr: preview(result.stderr),
          details: "Open the Alysis Code Output panel for full output."
        });
        statusBar.setError("Alysis Code sandbox doctor failed");
        await vscode.window.showErrorMessage(message);
      }
    })
  );
}

export function registerShowBridgeHealthCommand(
  context: vscode.ExtensionContext,
  cliDiscovery: CliDiscovery,
  getConfig: () => AlysisConfig,
  output: vscode.OutputChannel,
  statusBar: AlysisStatusBar,
  runtime?: CockpitRuntimeState,
  ensureRuntimeReady?: () => Promise<void>
): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("alysis.showBridgeHealth", async () => {
      await ensureRuntimeReady?.();
      const config = getConfig();
      runtime?.applyConfig(config);
      const guard = evaluateProcessExecutionCommand(config, "showBridgeHealth");
      if (!guard.allowed) {
        const message = guard.reason ?? "Alysis Code CLI execution is blocked by Workspace Trust.";
        runtime?.setCliOrigin("blocked", message);
        runtime?.setBridgeProcess("error", message);
        runtime?.recordEvent({
          severity: "error",
          source: "bridge",
          title: "Bridge health blocked",
          message
        });
        statusBar.setError(message);
        output.clear();
        output.appendLine(message);
        output.show(true);
        await vscode.window.showWarningMessage(message);
        return;
      }
      runtime?.setCliHealth("checking", "Checking Alysis Code CLI and IDE bridge health.");
      runtime?.setBridgeProcess("starting", "Checking Alysis Code IDE bridge health.");
      const detection = await withCommandProgress(
        "Alysis Code is checking bridge health",
        () => cliDiscovery.detect(config)
      );
      runtime?.applyDetectionResult(detection);
      if (detection.ok) {
        const health = detection.health;
        statusBar.setBridgeOk();
        output.clear();
        output.appendLine(JSON.stringify(health, null, 2));
        runtime?.recordEvent({
          severity: "info",
          source: "bridge",
          title: "Bridge health ok",
          message: `Protocol ${health.protocol_version}, Alysis Code ${health.alysis_version}.`
        });
        await vscode.window.showInformationMessage(
          `Alysis Code bridge ok: protocol ${health.protocol_version}, Alysis Code ${health.alysis_version}.`
        );
        return;
      }

      if (detection.code === "cli_missing") {
        statusBar.setMissingCli();
      } else {
        statusBar.setError("Alysis Code bridge health failed");
      }
      runtime?.setCliHealth(cliHealthFromFailureCode(detection.code), detection.message);
      runtime?.setBridgeProtocol(protocolStatusFromCode(detection.code), detection.message);
      // FE-10: no standalone "Bridge health failed" event — applyDetectionResult already drove the
      // canonical recovery card; the modal + Output below carry the detail for this manual check.
      output.clear();
      output.appendLine(detection.message);
      output.show(true);
      await vscode.window.showErrorMessage(detection.message);
    })
  );
}

/**
 * Both CLI probes below block for up to 15s. Show the standard progress notification so the window
 * never looks idle; the probes are bounded by their own timeouts, so no cancel affordance is offered.
 */
function withCommandProgress<T>(title: string, task: () => Promise<T>): Promise<T> {
  return Promise.resolve(
    vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title, cancellable: false },
      () => task()
    )
  );
}

function commandErrorMessage(error: unknown): string {
  let message: string;
  if (error instanceof BridgeHealthError) {
    message = `${error.message} (${error.code})`;
  } else if (error instanceof Error) {
    message = error.message;
  } else {
    message = String(error);
  }
  return redactForDisplay(message);
}

function errorCode(error: unknown): string {
  if (error instanceof BridgeHealthError) {
    return error.code;
  }
  if (error instanceof Error && "code" in error && typeof (error as NodeJS.ErrnoException).code === "string") {
    return (error as NodeJS.ErrnoException).code ?? "";
  }
  return "";
}

function preview(value: string): string {
  const redacted = redactForDisplay(value.trim());
  if (redacted.length <= 1200) {
    return redacted;
  }
  return `${redacted.slice(0, 1200)}\n[truncated]`;
}
