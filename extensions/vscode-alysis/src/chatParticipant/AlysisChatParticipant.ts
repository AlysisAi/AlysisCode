import * as vscode from "vscode";

import { CockpitRuntimeState } from "../chat/CockpitRuntimeState";
import { redactForDisplay } from "../client/CliDiscovery";
import { COMMANDS } from "../commands/registry";
import { SlashCommandResult, SlashCommandRouter } from "../slash/SlashCommandRouter";

export const ALYSIS_CHAT_PARTICIPANT_ID = "alysisai.alysis";

export interface AlysisChatParticipantDependencies {
  openCockpit(): Promise<void>;
  slashRouter: Pick<SlashCommandRouter, "execute">;
  runtime?: Pick<CockpitRuntimeState, "snapshot">;
}

export interface NativeChatRequestLike {
  readonly prompt: string;
  readonly command?: string;
}

export interface NativeChatResponseStreamLike {
  markdown(value: string): void;
  progress?(value: string): void;
  button?(command: vscode.Command): void;
}

export class AlysisChatParticipantRouter {
  public constructor(private readonly dependencies: AlysisChatParticipantDependencies) {}

  public async handle(
    request: NativeChatRequestLike,
    stream: NativeChatResponseStreamLike,
    token?: Pick<vscode.CancellationToken, "isCancellationRequested">
  ): Promise<vscode.ChatResult> {
    if (token?.isCancellationRequested) {
      return { metadata: { command: "cancelled" } };
    }
    const route = routeNativeChatRequest(request);
    try {
      switch (route.kind) {
        case "help":
          await this.openCockpit(stream);
          stream.markdown(helpMarkdown());
          this.addCockpitButton(stream);
          return { metadata: { command: "help" } };
        case "doctor":
          return await this.routeSlashCommand(stream, "/doctor", "doctor");
        case "unknownSlash":
          stream.markdown(
            safeMarkdown(
              "Unknown @alysis slash command. Supported commands: /help and /doctor."
            )
          );
          this.addCockpitButton(stream);
          return {
            metadata: { command: "unknown" },
            errorDetails: { message: "Unknown @alysis slash command." }
          };
        case "openCockpit":
          await this.openCockpit(stream);
          stream.markdown(
            safeMarkdown(
              "Opened the Alysis Code conversation. Use the sidebar to chat; approvals, artifacts, and diagnostics open as focused details when needed."
            )
          );
          this.addCockpitButton(stream);
          return { metadata: { command: "openCockpit" } };
      }
    } catch (error) {
      const message = safeErrorMessage(error);
      stream.markdown(safeMarkdown("Alysis Code could not handle this native chat request: " + message));
      this.addCockpitButton(stream);
      return {
        metadata: { command: route.kind },
        errorDetails: { message }
      };
    }
  }

  private async routeSlashCommand(
    stream: NativeChatResponseStreamLike,
    slashCommand: string,
    metadataCommand: string
  ): Promise<vscode.ChatResult> {
    await this.openCockpit(stream);
    stream.progress?.("Routing to Alysis Code.");
    const result = await this.dependencies.slashRouter.execute(slashCommand);
    stream.markdown(formatSlashResult(result));
    const recovery = this.runtimeRecoveryMessage();
    if (recovery) {
      stream.markdown("\n\n" + recovery);
    }
    this.addCockpitButton(stream);
    return {
      metadata: { command: metadataCommand, slashCommand },
      errorDetails: result.severity === "error" ? { message: redactForDisplay(result.notice) } : undefined
    };
  }

  private async openCockpit(stream: NativeChatResponseStreamLike): Promise<void> {
    stream.progress?.("Opening Alysis Code.");
    await this.dependencies.openCockpit();
  }

  private addCockpitButton(stream: NativeChatResponseStreamLike): void {
    stream.button?.({
      command: COMMANDS.openChat,
      title: "Open Alysis Code"
    });
  }

  private runtimeRecoveryMessage(): string {
    const runtime = this.dependencies.runtime?.snapshot();
    if (!runtime) {
      return "";
    }
    if (["missing", "unreachable", "incompatible", "broken"].includes(runtime.cliHealth.status)) {
      return safeMarkdown(
        [
          "CLI Health is " + runtime.cliHealth.status + ".",
          runtime.cliHealth.message || "Open Alysis Code Diagnostics for install or upgrade recovery.",
          "The extension does not forward API keys to missing, broken, or incompatible CLIs."
        ].join(" ")
      );
    }
    if (["incompatible", "missing_methods", "error"].includes(runtime.bridgeProtocol.status)) {
      return safeMarkdown(
        [
          "Bridge protocol is " + runtime.bridgeProtocol.status.replace(/_/g, " ") + ".",
          runtime.bridgeProtocol.message || "Open Alysis Code Diagnostics for exact missing methods."
        ].join(" ")
      );
    }
    return "";
  }
}

export function registerAlysisChatParticipant(
  context: vscode.ExtensionContext,
  dependencies: AlysisChatParticipantDependencies
): void {
  if (!vscode.chat?.createChatParticipant) {
    return;
  }
  const router = new AlysisChatParticipantRouter(dependencies);
  const participant = vscode.chat.createChatParticipant(
    ALYSIS_CHAT_PARTICIPANT_ID,
    (request, _chatContext, stream, token) => router.handle(request, stream, token)
  );
  participant.iconPath = vscode.Uri.joinPath(context.extensionUri, "resources", "alysis-logo.png");
  context.subscriptions.push(participant);
}

function routeNativeChatRequest(request: NativeChatRequestLike):
  | { kind: "help" }
  | { kind: "doctor" }
  | { kind: "unknownSlash" }
  | { kind: "openCockpit" } {
  const command = (request.command || "").trim().toLowerCase();
  const prompt = (request.prompt || "").trim();
  if (command === "help") {
    return { kind: "help" };
  }
  if (command === "doctor") {
    return { kind: "doctor" };
  }
  if (command) {
    return { kind: "unknownSlash" };
  }
  if (prompt.startsWith("/")) {
    return routePromptSlash(prompt);
  }
  return { kind: "openCockpit" };
}

function routePromptSlash(prompt: string):
  | { kind: "help" }
  | { kind: "doctor" }
  | { kind: "unknownSlash" } {
  const normalized = prompt.replace(/\s+/g, " ").trim();
  const lower = normalized.toLowerCase();
  if (lower === "/help") {
    return { kind: "help" };
  }
  if (lower === "/doctor") {
    return { kind: "doctor" };
  }
  return { kind: "unknownSlash" };
}

function helpMarkdown(): string {
  return [
    "The Alysis Code sidebar is the primary conversation. Approvals, artifacts, and diagnostics open as focused details when needed.",
    "",
    "Native @alysis commands:",
    "",
    "- `/help` opens Alysis Code and shows this help.",
    "- `/doctor` checks the local Alysis Code setup.",
    "",
    "Native chat does not auto-approve actions or parse terminal output."
  ].join("\n");
}

function formatSlashResult(result: SlashCommandResult): string {
  const prefix =
    result.severity === "error"
      ? "Alysis Code command failed: "
      : result.severity === "warning"
        ? "Alysis Code command warning: "
        : "Alysis Code command routed: ";
  return safeMarkdown(prefix + redactForDisplay(result.notice));
}

function safeErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return redactForDisplay(error.message);
  }
  return redactForDisplay(String(error));
}

function safeMarkdown(value: string): string {
  return redactForDisplay(value)
    .replace(/\\/g, "\\\\")
    .replace(/`/g, "\\`")
    .replace(/\[/g, "\\[")
    .replace(/\]/g, "\\]")
    .replace(/\(/g, "\\(")
    .replace(/\)/g, "\\)")
    .replace(/</g, "\\<")
    .replace(/>/g, "\\>");
}
