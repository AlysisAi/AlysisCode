import {
  ParsedSlashCommand,
  SLASH_COMMANDS,
  SlashCommandDefinition,
  closestSlashCommands,
  parseSlashCommand,
  slashHelpText
} from "./SlashCommandRegistry";

export type SlashMode = "readonly" | "review" | "auto";

export interface SlashCommandHandlers {
  isWorkspaceTrusted(): boolean;
  setMode(mode: SlashMode): Promise<string | void>;
  pickPermissions?(): Promise<SlashMode | undefined>;
  /** Bare (undefined name) opens the persona picker; a name sets it directly. Returns the outcome notice. */
  persona?(name?: string): Promise<string | void>;
  cancel(): Promise<void>;
  doctor(): Promise<void>;
  /** Opens the provider/model picker. Returns the outcome notice, or void for the legacy message. */
  config(): Promise<string | void>;
  /** Bare (undefined) opens the picker; a name sets the model directly. Returns the outcome notice. */
  pickModel?(model?: string): Promise<string | void>;
  backendAction(actionId: string, args: string): Promise<string>;
  commandCatalog?(): readonly SlashCommandDefinition[];
}

export interface SlashCommandResult {
  handled: boolean;
  command: string;
  notice: string;
  severity: "info" | "warning" | "error";
}

export class SlashCommandRouter {
  public constructor(private readonly handlers: SlashCommandHandlers) {}

  public parse(input: string): ParsedSlashCommand {
    return parseSlashCommand(input);
  }

  public catalog(): readonly SlashCommandDefinition[] {
    return this.handlers.commandCatalog?.() ?? SLASH_COMMANDS;
  }

  public async execute(input: string): Promise<SlashCommandResult> {
    const parsed = parseSlashCommand(input);
    if (["mode", "persona"].includes(parsed.id)
      && !this.catalog().some((definition) => definition.id === parsed.id)) {
      return result(parsed.command, "This command is unavailable in the current task or connected agent. Run /help to see available commands.", "warning");
    }
    switch (parsed.id) {
      case "help":
        return result(parsed.command, slashHelpText(this.catalog()));
      case "mode":
        return this.executeMode(parsed);
      case "persona":
        return this.executePersona(parsed);
      case "cancel":
        await this.handlers.cancel();
        return result(parsed.command, "Stopping the current run...");
      case "doctor":
        await this.handlers.doctor();
        return result(parsed.command, "Checking your setup.");
      case "config": {
        // An empty notice means the picker already reported the outcome (or the user escaped),
        // so the router stays quiet instead of claiming something happened.
        const notice = await this.handlers.config();
        return result(parsed.command, notice ?? "Opened provider settings.");
      }
      case "model":
        return this.executeModelPicker(parsed);
      case "backendAction":
        if (!parsed.backendActionId) {
          return result(parsed.command, "Backend action route is missing.", "error");
        }
        return this.executeBackendAction(parsed);
      case "unknown":
        return result(parsed.command, unknownCommandText(parsed.command, this.catalog()), "warning");
    }
  }

  private async executeMode(parsed: ParsedSlashCommand): Promise<SlashCommandResult> {
    const aliases: Record<string, SlashMode> = { "1": "review", safe: "review", review: "review", "2": "auto", fast: "auto", auto: "auto", "3": "readonly", read: "readonly", readonly: "readonly", ro: "readonly" };
    const argument = parsed.args.trim().toLowerCase();
    if (!argument && this.handlers.pickPermissions) {
      const choice = await this.handlers.pickPermissions();
      return choice ? this.executeMode({ ...parsed, args: choice }) : result(parsed.command, "");
    }
    if (["4", "full", "fullaccess", "full-access", "full_access"].includes(argument)) {
      return result(parsed.command, "Full access is available in the CLI, but the IDE bridge supports only read-only, review, and auto permissions.", "warning");
    }
    const mode = aliases[argument];
    if (mode !== "readonly" && mode !== "review" && mode !== "auto") {
      return result(parsed.command, "Usage: /permissions readonly, /permissions review, or /permissions auto.", "warning");
    }
    if (mode !== "readonly" && !this.handlers.isWorkspaceTrusted()) {
      return result(parsed.command, `Workspace Trust is required before switching Alysis Code to ${mode} mode. Use /permissions readonly in untrusted workspaces.`, "warning");
    }
    const notice = await this.handlers.setMode(mode);
    // FE-18: an active-session mode change runs the session.setMode backend action, which shows its own
    // result card and returns "" (so there is no duplicate notice). Only fall back to the default-mode
    // message when there was NO active session to change (setMode returned undefined) — in that case the
    // notice is the only feedback, and the "new sessions" wording is the accurate one.
    return result(
      parsed.command,
      notice === undefined ? `Default permissions set to ${modeLabel(mode)}. New tasks will use these permissions.` : notice
    );
  }

  private async executePersona(parsed: ParsedSlashCommand): Promise<SlashCommandResult> {
    if (!this.handlers.persona) {
      return result(parsed.command, "Personas are unavailable until the Alysis Code extension finishes initialization.", "warning");
    }
    const name = parsed.args.trim();
    if (name && !/^[a-z0-9][a-z0-9._-]{0,63}$/i.test(name)) {
      return result(parsed.command, "Usage: /persona [name] — names use letters, numbers, dots, dashes, and underscores.", "warning");
    }
    // No trust gate: the CLI clamp rule means a persona can only narrow the session's execution
    // mode, never widen it, so this mirrors the trust posture of session.setModel.
    const notice = await this.handlers.persona(name || undefined);
    // An empty notice means the outcome was already surfaced (picker dismissed or an error card).
    return result(parsed.command, notice ?? "");
  }

  private async executeModelPicker(parsed: ParsedSlashCommand): Promise<SlashCommandResult> {
    if (!this.handlers.pickModel) {
      return result(
        parsed.command,
        "The model picker is unavailable until the Alysis Code extension finishes initialization.",
        "warning"
      );
    }
    const notice = await this.handlers.pickModel(parsed.args.trim() || undefined);
    return result(parsed.command, notice ?? "");
  }

  private async executeBackendAction(parsed: ParsedSlashCommand): Promise<SlashCommandResult> {
    if (!parsed.backendActionId) {
      return result(parsed.command, "Backend action route is missing.", "error");
    }
    if (!this.catalog().some((command) => command.backendActionId === parsed.backendActionId)) {
      return result(
        parsed.command,
        `${parsed.command} is not available for the current workspace, session, or connected Alysis Code CLI.`,
        "warning"
      );
    }
    const notice = await this.handlers.backendAction(parsed.backendActionId, parsed.args);
    return result(parsed.command, notice);
  }
}

/** The same names the composer's Permissions picker uses, so a notice never says "readonly" or "auto". */
function modeLabel(mode: "readonly" | "review" | "auto"): string {
  if (mode === "readonly") {
    return "Read-only";
  }
  return mode === "auto" ? "Auto-approve" : "Review changes";
}

function result(command: string, notice: string, severity: "info" | "warning" | "error" = "info"): SlashCommandResult {
  return { handled: true, command, notice, severity };
}

function unknownCommandText(command: string, commands: readonly SlashCommandDefinition[]): string {
  if (command === "/mode") return "Use /permissions to choose what Alysis Code may do, or /persona to choose how it works.";
  if (command === "/plan") return "Chat plan mode has been removed. Use /persona architect for planning.";
  if (command === "/subagent") return "Use /subagents to inspect or configure subagents.";
  if (command === "/ask") return "One-turn /ask is currently CLI-only. In VS Code, /persona ask selects read-only Q&A for subsequent turns; switch back with /persona code when finished.";
  if (command === "/chat") return "/chat has been retired. Type a message directly, or use /persona ask for read-only Q&A.";
  const suggestions = closestSlashCommands(command, 3, commands);
  return [
    `Unknown slash command: ${command || "/"}`,
    suggestions.length > 0 ? `Closest commands: ${suggestions.join(", ")}` : "",
    "Run /help to see supported Alysis Code slash commands."
  ].filter(Boolean).join("\n");
}
