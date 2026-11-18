import { BACKEND_ACTIONS } from "../backend/BackendActionMetadata";
import { BridgeCompatibilitySnapshot, CompatibilityFeatureId } from "../client/compatibility";

export type SlashCommandId =
  | "help"
  | "mode"
  | "persona"
  | "cancel"
  | "doctor"
  | "config"
  | "model"
  | "backendAction";

export interface SlashCommandDefinition {
  id: SlashCommandId;
  command: string;
  aliases?: string[];
  title: string;
  description: string;
  usage: string;
  trust: "safe" | "requiresTrust" | "controllerGuarded";
  backendActionId?: string;
}

export interface SlashCommandCatalogContext {
  workspaceTrusted: boolean;
  activeSession: boolean;
  activeRun: boolean;
  personasAvailable?: boolean;
  compatibility?: BridgeCompatibilitySnapshot;
  isBackendActionSupported(actionId: string): boolean;
}

export interface ParsedSlashCommand {
  id: SlashCommandId | "unknown";
  raw: string;
  command: string;
  args: string;
  backendActionId?: string;
}

export interface SlashSuggestion {
  command: string;
  title: string;
  description: string;
  usage: string;
}

const CORE_SLASH_COMMANDS: readonly SlashCommandDefinition[] = [
  {
    id: "help",
    command: "/help",
    title: "Help",
    description: "List every slash command.",
    usage: "/help",
    trust: "safe"
  },
  {
    id: "mode",
    command: "/permissions",
    title: "Permissions",
    description: "Choose read-only access, reviewed changes, or automatic actions within safeguards.",
    usage: "/permissions [readonly|review|auto]",
    trust: "controllerGuarded"
  },
  {
    id: "persona",
    command: "/persona",
    title: "Persona",
    description: "Switch persona. A persona can narrow what the agent may do, never widen it.",
    usage: "/persona [name]",
    trust: "controllerGuarded"
  },
  {
    id: "cancel",
    command: "/cancel",
    title: "Cancel",
    description: "Stop the running task at the next safe point.",
    usage: "/cancel",
    trust: "controllerGuarded"
  },
  {
    id: "doctor",
    command: "/doctor",
    title: "Doctor",
    description: "Check the local setup: engine, provider, and sandbox.",
    usage: "/doctor",
    trust: "controllerGuarded"
  },
  {
    id: "config",
    command: "/config",
    title: "Config",
    description: "Connect a provider and choose a model; keys are stored securely.",
    usage: "/config",
    trust: "safe"
  },
  {
    id: "model",
    command: "/model",
    title: "Model",
    description: "Choose a model, or set one directly by name.",
    usage: "/model [name]",
    // Deliberately not gated on an active session. Choosing a model is the first thing most
    // people do on a cold open, which is precisely when no session exists yet; the catalog
    // controller enforces Workspace Trust for the mutations that actually need it.
    trust: "controllerGuarded"
  }
];

const CORE_SLASH_SPELLINGS: ReadonlySet<string> = new Set(
  CORE_SLASH_COMMANDS.flatMap((definition) => [definition.command, ...(definition.aliases ?? [])])
    .map((spelling) => spelling.toLowerCase())
);

export const SLASH_COMMANDS: readonly SlashCommandDefinition[] = [
  ...CORE_SLASH_COMMANDS,
  // A backend alias that collides with a core spelling is dropped rather than listed twice.
  // parseSlashCommand already resolves core routes first, so a duplicate entry would only ever
  // show up as a phantom second row in /help and the suggestion list.
  ...BACKEND_ACTIONS.flatMap((action) =>
    action.slashAliases.filter((alias) => !CORE_SLASH_SPELLINGS.has(alias.toLowerCase())).map((alias): SlashCommandDefinition => ({
      id: "backendAction",
      command: alias,
      title: action.title,
      description: action.description,
      usage: slashUsage(alias, action.parameterStrategy),
      trust: action.workspaceTrustRequired ? "requiresTrust" : "controllerGuarded",
      backendActionId: action.id
    }))
  )
];

/** Project only routes that are dispatchable in the current host/capability context. */
export function availableSlashCommands(context: SlashCommandCatalogContext): SlashCommandDefinition[] {
  const supports = (feature: CompatibilityFeatureId): boolean => !context.compatibility
    || (context.compatibility.protocol.compatible && context.compatibility.features[feature]?.supported === true);
  return SLASH_COMMANDS.filter((definition) => {
    if (definition.id === "backendAction") {
      const action = BACKEND_ACTIONS.find((candidate) => candidate.id === definition.backendActionId);
      return action !== undefined
        && context.isBackendActionSupported(action.id)
        && (!action.workspaceTrustRequired || context.workspaceTrusted)
        && (!action.requiresActiveSession || context.activeSession);
    }
    if (definition.id === "mode") return !context.activeRun;
    if (definition.id === "persona") return context.activeSession && !context.activeRun
      && context.personasAvailable !== false && supports("personas");
    if (definition.id === "cancel") {
      return context.activeRun;
    }
    return true;
  });
}

export function parseSlashCommand(input: string): ParsedSlashCommand {
  const raw = input.trim();
  if (!raw.startsWith("/")) {
    return { id: "unknown", raw, command: "", args: raw };
  }
  const normalized = raw.replace(/\s+/g, " ");
  const lower = normalized.toLowerCase();

  if (lower === "/help") {
    return parsed("help", raw, "/help", "");
  }
  if (lower === "/cancel") {
    return parsed("cancel", raw, "/cancel", "");
  }
  if (lower === "/doctor") {
    return parsed("doctor", raw, "/doctor", "");
  }
  if (lower === "/config") {
    return parsed("config", raw, "/config", "");
  }
  // Exact-match only, so "/model-info" still resolves to the session.modelInfo backend alias.
  if (lower === "/model") {
    return parsed("model", raw, "/model", "");
  }
  if (lower.startsWith("/model ")) {
    return parsed("model", raw, "/model", raw.slice(raw.indexOf(" ") + 1).trim());
  }
  if (lower === "/permissions") {
    return parsed("mode", raw, "/permissions", "");
  }
  if (lower.startsWith("/permissions ")) {
    return parsed("mode", raw, "/permissions", raw.slice(raw.indexOf(" ") + 1).trim());
  }
  if (lower === "/persona") {
    return parsed("persona", raw, "/persona", "");
  }
  if (lower.startsWith("/persona ")) {
    return parsed("persona", raw, "/persona", raw.slice(raw.indexOf(" ") + 1).trim());
  }
  const backendCommand = backendSlashAliases()
    .sort((a, b) => b.alias.length - a.alias.length)
    .find((candidate) => lower === candidate.alias || lower.startsWith(`${candidate.alias} `));
  if (backendCommand) {
    const args = lower === backendCommand.alias
      ? ""
      : raw.slice(raw.toLowerCase().indexOf(backendCommand.alias) + backendCommand.alias.length).trim();
    return parsed("backendAction", raw, backendCommand.alias, args, backendCommand.actionId);
  }

  const command = raw.split(/\s+/, 1)[0] ?? raw;
  return { id: "unknown", raw, command, args: "" };
}

export function slashCommandSuggestions(
  input: string,
  limit = 8,
  commands: readonly SlashCommandDefinition[] = SLASH_COMMANDS
): SlashSuggestion[] {
  const query = input.trim().toLowerCase();
  const options = commands.flatMap((definition) => [
    suggestion(definition, definition.command),
    ...(definition.aliases ?? []).map((alias) => suggestion(definition, alias))
  ]);
  if (!query || query === "/") {
    return options.slice(0, limit);
  }
  return options
    .filter((option) => option.command.toLowerCase().includes(query))
    .slice(0, limit);
}

export function closestSlashCommands(
  command: string,
  limit = 3,
  commands: readonly SlashCommandDefinition[] = SLASH_COMMANDS
): string[] {
  const normalized = command.toLowerCase();
  return commands.flatMap((definition) => [definition.command, ...(definition.aliases ?? [])])
    .map((candidate) => ({ candidate, distance: levenshtein(normalized, candidate.toLowerCase()) }))
    .sort((a, b) => a.distance - b.distance || a.candidate.localeCompare(b.candidate))
    .slice(0, limit)
    .map((item) => item.candidate);
}

export function slashHelpText(commands: readonly SlashCommandDefinition[] = SLASH_COMMANDS): string {
  return [
    "Alysis Code slash commands:",
    "",
    ...commands.map((command) => `${command.usage} - ${command.description}`),
    "",
    "Commands that change files or settings require Workspace Trust."
  ].join("\n");
}

function parsed(
  id: SlashCommandId,
  raw: string,
  command: string,
  args: string,
  backendActionId?: string
): ParsedSlashCommand {
  return { id, raw, command, args, backendActionId };
}

function suggestion(definition: SlashCommandDefinition, command: string): SlashSuggestion {
  return {
    command,
    title: definition.title,
    description: definition.description,
    usage: definition.usage
  };
}

function backendSlashAliases(): Array<{ alias: string; actionId: string }> {
  return BACKEND_ACTIONS.flatMap((action) =>
    action.slashAliases.map((alias) => ({ alias: alias.toLowerCase(), actionId: action.id }))
  );
}

function slashUsage(alias: string, parameterStrategy: string): string {
  switch (parameterStrategy) {
    case "query":
      return `${alias} <query>`;
    case "target-session-id":
      return `${alias} <session_id>`;
    case "model":
      return `${alias} <model>`;
    case "stream-on-off":
      return `${alias} on|off`;
    case "workspace-relative-path":
      return `${alias} <path>`;
    case "image-path":
      return `${alias} [path]`;
    case "skill-name":
    case "tool-name":
    case "profile-name":
    case "extension-id":
      return `${alias} <name_or_id>`;
    case "extension-source":
      return `${alias} <path_or_url>`;
    case "feedback":
      return `${alias} [feedback]`;
    case "optional-focus":
      return `${alias} [focus]`;
    case "config-key-value":
      return `${alias} <key=value>`;
    case "update-check":
      return `${alias} [cached|online]`;
    case "optional-model":
      return `${alias} [model]`;
    case "subagent-status-or-toggle":
      return `${alias} status|on|off`;
    case "trace-status-or-level":
      return `${alias} [off|compact|full|events|clear]`;
    case "terminals-command":
      return `${alias} [list|show <id>|kill <id>|clear <id>]`;
    default:
      return alias;
  }
}

function levenshtein(a: string, b: string): number {
  const previous = Array.from({ length: b.length + 1 }, (_, index) => index);
  const current = Array.from({ length: b.length + 1 }, () => 0);
  for (let i = 1; i <= a.length; i += 1) {
    current[0] = i;
    for (let j = 1; j <= b.length; j += 1) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      current[j] = Math.min(
        current[j - 1] + 1,
        previous[j] + 1,
        previous[j - 1] + cost
      );
    }
    for (let j = 0; j <= b.length; j += 1) {
      previous[j] = current[j];
    }
  }
  return previous[b.length];
}
