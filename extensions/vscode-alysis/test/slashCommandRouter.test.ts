import assert from "node:assert/strict";
import test from "node:test";

import {
  SLASH_COMMANDS,
  availableSlashCommands,
  parseSlashCommand,
  slashCommandSuggestions
} from "../src/slash/SlashCommandRegistry";
import { SlashCommandHandlers, SlashCommandRouter } from "../src/slash/SlashCommandRouter";
import { routeSlashModeChange } from "../src/slash/modeRouting";

test("SlashCommandRouter parses every supported command", () => {
  const cases: Array<[string, string]> = [
    ["/help", "help"],
    ["/permissions", "mode"],
    ["/permissions readonly", "mode"],
    ["/permissions review", "mode"],
    ["/permissions auto", "mode"],
    ["/persona", "persona"],
    ["/persona architect", "persona"],
    ["/cancel", "cancel"],
    ["/doctor", "doctor"],
    ["/config", "config"],
    ["/config set default_model=gpt-5", "backendAction"],
    ["/update", "backendAction"],
    ["/update online", "backendAction"],
    ["/usage", "backendAction"],
    ["/history pytest", "backendAction"],
    ["/context", "backendAction"],
    ["/ctx", "backendAction"],
    ["/compact failing tests", "backendAction"],
    ["/resume 20260601T000000Z_abcd1234", "backendAction"],
    // /model is a core route (the provider-and-model picker); /model-info stays a backend action.
    ["/model", "model"],
    ["/model gpt-5", "model"],
    ["/model-info", "backendAction"],
    ["/model-info gpt-5", "backendAction"],
    ["/subagents status", "backendAction"],
    ["/subagents on", "backendAction"],
    ["/subagents off", "backendAction"],
    ["/trace", "backendAction"],
    ["/trace full", "backendAction"],
    ["/trace events", "backendAction"],
    ["/terminals", "backendAction"],
    ["/terminals list", "backendAction"],
    ["/terminals show proc-1", "backendAction"],
    ["/terminals kill proc-1", "backendAction"],
    ["/terminals clear proc-1", "backendAction"],
    ["/stream off", "backendAction"],
    ["/cd src", "backendAction"],
    ["/pwd", "backendAction"],
    ["/status", "backendAction"],
    ["/clear", "backendAction"],
    ["/image screenshots/fail.png", "backendAction"],
    ["/paste-image", "backendAction"],
    ["/images", "backendAction"],
    ["/clear-images", "backendAction"],
    ["/skills", "backendAction"],
    ["/skill python", "backendAction"],
    ["/tools", "backendAction"],
    ["/hooks", "backendAction"],
    ["/mcp", "backendAction"],
    ["/profiles", "backendAction"],
    ["/profile use demo", "backendAction"],
    ["/report", "backendAction"],
    ["/feedback", "backendAction"]
  ];

  for (const [input, id] of cases) {
    assert.equal(parseSlashCommand(input).id, id, input);
  }
  // The retired plan-and-execute commands are no longer part of the catalog.
  for (const input of ["/forge plan build a thing", "/execute", "/plans", "/open plan", "/diffs", "/asset list", "/review T01"]) {
    assert.equal(parseSlashCommand(input).id, "unknown", input);
  }
});

test("new backend slash commands route to backend action handler with args", async () => {
  const calls: Array<{ actionId: string; args: string }> = [];
  const router = new SlashCommandRouter(
    handlers({
      backendAction: async (actionId, args) => {
        calls.push({ actionId, args });
        return `ran ${actionId}`;
      }
    })
  );

  assert.equal((await router.execute("/usage")).notice, "ran session.usage");
  await router.execute("/history pytest -q");
  await router.execute("/ctx");
  await router.execute("/status");
  await router.execute("/clear");
  await router.execute("/image screenshots/fail.png");
  await router.execute("/paste-image screenshots/paste.png");
  await router.execute("/images");
  await router.execute("/clear-images");
  await router.execute("/model-info gpt-5");
  await router.execute("/subagents on");
  await router.execute("/trace full");
  await router.execute("/terminals show proc-1");
  await router.execute("/terminals kill proc-1");
  await router.execute("/update online");
  await router.execute("/config set default_model=gpt-5");
  await router.execute("/profile use demo");
  await router.execute("/feedback follow-up");

  assert.deepEqual(calls, [
    { actionId: "session.usage", args: "" },
    { actionId: "session.history", args: "pytest -q" },
    { actionId: "session.context", args: "" },
    { actionId: "session.status", args: "" },
    { actionId: "session.clear", args: "" },
    { actionId: "session.images.add", args: "screenshots/fail.png" },
    { actionId: "session.images.add", args: "screenshots/paste.png" },
    { actionId: "session.images.list", args: "" },
    { actionId: "session.images.clear", args: "" },
    { actionId: "session.modelInfo", args: "gpt-5" },
    { actionId: "session.subagents.status", args: "on" },
    { actionId: "session.trace.status", args: "full" },
    { actionId: "session.terminals.list", args: "show proc-1" },
    { actionId: "session.terminals.list", args: "kill proc-1" },
    { actionId: "update.check", args: "online" },
    { actionId: "config.set", args: "default_model=gpt-5" },
    { actionId: "profile.use", args: "demo" },
    { actionId: "report.create", args: "follow-up" }
  ]);
});

test("normal-agent slash commands route to native handlers or typed backend actions", async () => {
  const backendCalls: Array<{ actionId: string; args: string }> = [];
  const coreCalls: string[] = [];
  const router = new SlashCommandRouter(
    handlers({
      setMode: async (mode) => {
        coreCalls.push(`mode:${mode}`);
        return `mode:${mode}`;
      },
      doctor: async () => {
        coreCalls.push("doctor");
      },
      config: async () => {
        coreCalls.push("config");
      },
      pickModel: async (model) => {
        coreCalls.push(`model:${model ?? ""}`);
        return `model:${model ?? ""}`;
      },
      backendAction: async (actionId, args) => {
        backendCalls.push({ actionId, args });
        return `ran ${actionId}`;
      }
    })
  );

  for (const command of [
    "/permissions review",
    "/model gpt-5",
    "/model-info",
    "/model-info gpt-5-mini",
    "/stream on",
    "/stream off",
    "/subagents",
    "/subagents status",
    "/subagents on",
    "/subagents off",
    "/usage",
    "/history pytest",
    "/context",
    "/ctx",
    "/compact failing tests",
    "/resume retained-1",
    "/clear",
    "/cd src",
    "/pwd",
    "/image screenshots/failure.png",
    "/paste-image screenshots/paste.png",
    "/images",
    "/clear-images",
    "/doctor",
    "/config",
    "/config set default_model=gpt-5",
    "/profiles",
    "/profile demo",
    "/profile use demo",
    "/trace",
    "/trace events",
    "/trace full",
    "/terminals",
    "/terminals list",
    "/terminals show proc-1",
    "/terminals kill proc-1",
    "/terminals clear proc-1"
  ]) {
    await router.execute(command);
  }

  assert.deepEqual(coreCalls, ["mode:review", "model:gpt-5", "doctor", "config"]);
  assert.deepEqual(backendCalls, [
    { actionId: "session.modelInfo", args: "" },
    { actionId: "session.modelInfo", args: "gpt-5-mini" },
    { actionId: "session.setStream", args: "on" },
    { actionId: "session.setStream", args: "off" },
    { actionId: "session.subagents.status", args: "" },
    { actionId: "session.subagents.status", args: "status" },
    { actionId: "session.subagents.status", args: "on" },
    { actionId: "session.subagents.status", args: "off" },
    { actionId: "session.usage", args: "" },
    { actionId: "session.history", args: "pytest" },
    { actionId: "session.context", args: "" },
    { actionId: "session.context", args: "" },
    { actionId: "session.compact", args: "failing tests" },
    { actionId: "session.resume", args: "retained-1" },
    { actionId: "session.clear", args: "" },
    { actionId: "session.setActiveWorkdir", args: "src" },
    { actionId: "session.status", args: "" },
    { actionId: "session.images.add", args: "screenshots/failure.png" },
    { actionId: "session.images.add", args: "screenshots/paste.png" },
    { actionId: "session.images.list", args: "" },
    { actionId: "session.images.clear", args: "" },
    { actionId: "config.set", args: "default_model=gpt-5" },
    { actionId: "profile.list", args: "" },
    { actionId: "profile.show", args: "demo" },
    { actionId: "profile.use", args: "demo" },
    { actionId: "session.trace.status", args: "" },
    { actionId: "session.trace.status", args: "events" },
    { actionId: "session.trace.status", args: "full" },
    { actionId: "session.terminals.list", args: "" },
    { actionId: "session.terminals.list", args: "list" },
    { actionId: "session.terminals.list", args: "show proc-1" },
    { actionId: "session.terminals.list", args: "kill proc-1" },
    { actionId: "session.terminals.list", args: "clear proc-1" }
  ]);
});

test("bare argument-taking commands return usage without calling handlers", async () => {
  const router = new SlashCommandRouter(
    handlers({
      setMode: async () => assert.fail("bare /permissions must not mutate settings")
    })
  );

  assert.match((await router.execute("/permissions")).notice, /Usage: \/permissions readonly, \/permissions review, or \/permissions auto/);
});

test("/permissions auto applies when the workspace is trusted and is trust-gated otherwise", async () => {
  const calls: string[] = [];
  const trusted = new SlashCommandRouter(
    handlers({ trusted: true, setMode: async (mode) => { calls.push(`mode:${mode}`); } })
  );
  const outcome = await trusted.execute("/permissions auto");
  assert.deepEqual(calls, ["mode:auto"]);
  assert.match(outcome.notice, /Default permissions set to Auto-approve/);

  const untrusted = new SlashCommandRouter(
    handlers({ trusted: false, setMode: async () => assert.fail("/permissions auto must not mutate settings in an untrusted workspace") })
  );
  assert.match((await untrusted.execute("/permissions auto")).notice, /Workspace Trust is required/);
});

test("/permissions uses live-session notices when the handler changes an active session", async () => {
  const calls: string[] = [];
  const router = new SlashCommandRouter(
    handlers({
      setMode: async (mode) => {
        calls.push(`mode:${mode}`);
        return "Set Session Mode completed. Details were written to the Alysis Code output channel.";
      }
    })
  );

  const outcome = await router.execute("/permissions readonly");

  assert.deepEqual(calls, ["mode:readonly"]);
  assert.match(outcome.notice, /Set Session Mode completed/);
  assert.doesNotMatch(outcome.notice, /default mode set/);
});

test("FE-18: an active-session /permissions returns an empty notice (the result card is the only feedback)", async () => {
  // session.setMode now suppresses its "completed" notice (returns "") because its running -> ok result
  // card is the feedback. /permissions must keep that empty notice, NOT substitute the (misleading for an
  // active session) "default mode set" fallback — that fallback is only for the no-active-session case.
  const router = new SlashCommandRouter(handlers({ setMode: async () => "" }));
  const outcome = await router.execute("/permissions readonly");
  assert.equal(outcome.notice, "", "no duplicate notice — the session.setMode result card is the feedback");
});

test("routeSlashModeChange calls session.setMode for active sessions", async () => {
  const calls: Array<{ actionId: string; args: string }> = [];
  const notice = await routeSlashModeChange("review", {
    activeSessionId: () => "session-1",
    executeBackendAction: async (actionId, args) => {
      calls.push({ actionId, args });
      return "Set Session Mode completed.";
    },
    updateDefaultMode: async () => assert.fail("active session mode must not update default config")
  });

  assert.equal(notice, "Set Session Mode completed.");
  assert.deepEqual(calls, [{ actionId: "session.setMode", args: "review" }]);
});

test("routeSlashModeChange updates default config when no session exists", async () => {
  const defaults: string[] = [];
  const notice = await routeSlashModeChange("readonly", {
    activeSessionId: () => undefined,
    executeBackendAction: async () => {
      assert.fail("no-session mode must not call session.setMode");
    },
    updateDefaultMode: async (mode) => {
      defaults.push(mode);
    }
  });

  assert.equal(notice, undefined);
  assert.deepEqual(defaults, ["readonly"]);
});

test("/cancel, /doctor, and /config route to controller commands", async () => {
  const calls: string[] = [];
  const router = new SlashCommandRouter(
    handlers({
      cancel: async () => { calls.push("cancel"); },
      doctor: async () => { calls.push("doctor"); },
      config: async () => { calls.push("config"); }
    })
  );

  await router.execute("/cancel");
  await router.execute("/doctor");
  await router.execute("/config");

  assert.deepEqual(calls, ["cancel", "doctor", "config"]);
});

test("unknown slash commands fail closed with suggestions", async () => {
  const router = new SlashCommandRouter(
    handlers({
      backendAction: async () => assert.fail("unknown command must not call handlers")
    })
  );

  const result = await router.execute("/plna do not send this to model");

  assert.match(result.notice, /Unknown slash command: \/plna/);
  assert.match(result.notice, /Closest commands:/);
});

test("slash commands are blocked appropriately in untrusted workspaces", async () => {
  const calls: string[] = [];
  const router = new SlashCommandRouter(
    handlers({
      trusted: false,
      backendAction: async (actionId) => { calls.push(actionId); return ""; }
    })
  );

  assert.match((await router.execute("/permissions review")).notice, /Workspace Trust is required/);
  assert.deepEqual(calls, []);
});

test("slash suggestions filter command and alias text", () => {
  assert.equal(slashCommandSuggestions("/perm").some((item) => item.command === "/permissions"), true);
  assert.equal(slashCommandSuggestions("/cont").some((item) => item.command === "/context"), true);
  assert.equal(slashCommandSuggestions("/forge").length, 0);
});

test("slash catalogs contain one owner per token and only dispatchable context routes", async () => {
  const catalog = availableSlashCommands({
    workspaceTrusted: false,
    activeSession: false,
    activeRun: false,
    isBackendActionSupported: (actionId) => actionId !== "session.status"
  });
  const tokens = catalog.flatMap((definition) => [definition.command, ...(definition.aliases ?? [])]);
  assert.equal(new Set(tokens.map((token) => token.toLowerCase())).size, tokens.length);
  assert.equal(SLASH_COMMANDS.filter((definition) => definition.command === "/permissions").length, 1);
  assert.equal(catalog.some((definition) => definition.backendActionId === "session.status"), false);
  assert.equal(catalog.some((definition) => definition.backendActionId === "session.modelInfo"), false);

  let backendCalls = 0;
  const router = new SlashCommandRouter(handlers({
    commandCatalog: () => catalog,
    backendAction: async () => {
      backendCalls += 1;
      return "unexpected";
    }
  }));
  const help = await router.execute("/help");
  assert.doesNotMatch(help.notice, /\/model-info/);
  const stale = await router.execute("/model-info");
  assert.equal(stale.severity, "warning");
  assert.equal(backendCalls, 0);
});

test("slash command notices do not include SecretStorage API keys", async () => {
  const router = new SlashCommandRouter(handlers());

  const result = await router.execute("/unknown sk-test-secret-value");

  assert.doesNotMatch(result.notice, /sk-test-secret-value/);
});

test("/persona routes bare to the picker and a name directly to the setter", async () => {
  const calls: Array<string | undefined> = [];
  const router = new SlashCommandRouter(
    handlers({
      persona: async (name) => {
        calls.push(name);
        return name ? `Persona set: ${name} (review).` : "";
      }
    })
  );

  const bare = await router.execute("/persona");
  assert.deepEqual(calls, [undefined], "bare /persona opens the picker");
  assert.equal(bare.severity, "info");

  const named = await router.execute("/persona architect");
  assert.deepEqual(calls, [undefined, "architect"]);
  assert.equal(named.notice, "Persona set: architect (review).", "the outcome notice flows through");
});

test("/persona validates the name shape before it reaches any handler", async () => {
  const calls: Array<string | undefined> = [];
  const router = new SlashCommandRouter(handlers({ persona: async (name) => { calls.push(name); } }));

  const invalid = await router.execute("/persona ../evil name");
  assert.equal(invalid.severity, "warning");
  assert.match(invalid.notice, /Usage: \/persona/);
  assert.deepEqual(calls, [], "an invalid name never reaches the handler");
});

test("/persona without a wired handler fails closed with guidance", async () => {
  const router = new SlashCommandRouter(handlers());
  const result = await router.execute("/persona architect");
  assert.equal(result.severity, "warning");
  assert.match(result.notice, /unavailable/i);
});

test("/persona is in the registry catalog and the slash suggestions", () => {
  const definition = SLASH_COMMANDS.find((command) => command.command === "/persona");
  assert.ok(definition, "/persona is registered");
  assert.equal(definition?.usage, "/persona [name]");
  assert.match(definition?.description ?? "", /never widen/);
  const available = availableSlashCommands({
    workspaceTrusted: false,
    activeSession: false,
    activeRun: false,
    isBackendActionSupported: () => false
  });
  assert.equal(available.some((command) => command.command === "/persona"), false, "personas require a live session, even without a trust gate");
  assert.equal(
    slashCommandSuggestions("/pers").some((suggestion) => suggestion.command === "/persona"),
    true,
    "composer autocomplete sourced from the registry offers /persona"
  );
});

function handlers(overrides: Partial<SlashCommandHandlers> & { trusted?: boolean } = {}): SlashCommandHandlers {
  return {
    isWorkspaceTrusted: () => overrides.trusted ?? true,
    setMode: overrides.setMode ?? (async () => undefined),
    pickPermissions: overrides.pickPermissions,
    persona: overrides.persona,
    cancel: overrides.cancel ?? (async () => undefined),
    doctor: overrides.doctor ?? (async () => undefined),
    config: overrides.config ?? (async () => undefined),
    pickModel: overrides.pickModel,
    backendAction: overrides.backendAction ?? (async () => "Backend action completed."),
    commandCatalog: overrides.commandCatalog
  };
}

test("retired commands give migration guidance without starting an agent workflow", async () => {
  const router = new SlashCommandRouter(handlers({
    setMode: async () => assert.fail("retired mode commands must not change permissions"),
    backendAction: async () => assert.fail("retired aliases must not invoke backend actions")
  }));
  for (const [command, guidance] of [
    ["/plan on", /\/persona architect/], ["/plan off", /\/persona architect/],
    ["/mode auto", /\/permissions/], ["/subagent on", /\/subagents/],
    ["/ask explain this", /CLI.only/i], ["/chat hello", /retired/i]
  ] as const) {
    const outcome = await router.execute(command);
    assert.equal(outcome.severity, "warning", command);
    assert.match(outcome.notice, guidance, command);
  }
});

test("permissions accept current CLI choices, open a picker, and reject CLI-only full access", async () => {
  const modes: string[] = [];
  let picked: "readonly" | undefined = "readonly";
  const router = new SlashCommandRouter(handlers({
    setMode: async (mode) => { modes.push(mode); },
    pickPermissions: async () => picked
  }));
  for (const value of ["1", "safe", "2", "fast", "3", "read", "ro"]) await router.execute(`/permissions ${value}`);
  await router.execute("/permissions");
  assert.deepEqual(modes, ["review", "review", "auto", "auto", "readonly", "readonly", "readonly", "readonly"]);
  picked = undefined;
  assert.equal((await router.execute("/permissions")).notice, "");
  for (const value of ["4", "full", "fullaccess", "full-access", "full_access"]) {
    assert.match((await router.execute(`/permissions ${value}`)).notice, /CLI.*IDE bridge/);
  }
  assert.equal(modes.length, 8, "cancel and unsupported permissions never mutate the session");
});

test("disabled or unsupported core workflows cannot be reached through stale slash input", async () => {
  const context = { workspaceTrusted: true, activeSession: true, activeRun: false,
    personasAvailable: false, isBackendActionSupported: () => true };
  const catalog = availableSlashCommands(context);
  const router = new SlashCommandRouter(handlers({
    commandCatalog: () => catalog,
    persona: async () => assert.fail("disabled personas must not open"),
    backendAction: async () => assert.fail("retired commands must not dispatch")
  }));
  for (const command of ["/forge plan fix this", "/execute", "/persona ask"]) {
    assert.equal((await router.execute(command)).severity, "warning", command);
  }
  const unsupported = availableSlashCommands({ ...context, personasAvailable: true,
    compatibility: { protocol: { compatible: true }, features: {} } as any });
  assert.equal(unsupported.some((item) => item.id === "persona"), false);
  const running = availableSlashCommands({ ...context, personasAvailable: true, activeRun: true });
  assert.equal(running.some((item) => ["mode", "persona"].includes(item.id)), false);
});
