import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

import { JSDOM, VirtualConsole } from "jsdom";

const ROOT = resolve(__dirname, "../..");

/** Every mounted window is closed after the run so webview timers (ack timeout, countdowns) cannot keep the process alive. */
const mountedWindows: any[] = [];
test.after(() => {
  for (const window of mountedWindows) {
    try {
      window.close();
    } catch {
      // ignore
    }
  }
});

function mountStartView(): { window: any; messages: any[]; errors: any[] } {
  const provider = readFileSync(resolve(ROOT, "media/startView.html"), "utf8");
  const body = provider.match(/<body>([\s\S]*?)<script nonce/);
  if (!body) {
    throw new Error("could not extract Start Here body");
  }
  const errors: any[] = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (error) => errors.push(error));
  const dom = new JSDOM(`<!DOCTYPE html><html><body>${body[1]}</body></html>`, {
    runScripts: "outside-only",
    pretendToBeVisual: true,
    virtualConsole
  });
  const window: any = dom.window;
  const messages: any[] = [];
  window.acquireVsCodeApi = () => ({
    getState: () => undefined,
    setState: () => undefined,
    postMessage: (message: any) => messages.push(message)
  });
  window.eval(readFileSync(resolve(ROOT, "media/startView.js"), "utf8"));
  mountedWindows.push(window);
  return { window, messages, errors };
}

function modelsFixture(): Record<string, unknown> {
  return {
    supported: true,
    reason: "",
    loaded: true,
    busy: "",
    error: "",
    activeProfile: "anthropic",
    activeModel: "claude-sonnet-5",
    providers: [
      {
        key: "anthropic", label: "Anthropic Claude", host: "api.anthropic.com",
        protocolKind: "native", protocolLabel: "Native API",
        models: ["claude-opus-5", "claude-sonnet-5"], keyEnvVar: "ANTHROPIC_API_KEY",
        notes: "", warning: "", needsBaseUrl: false, local: false, recommended: true,
        profileName: "anthropic", connected: true
      },
      // The alias and the compatibility twin share Anthropic's host: one provider, three presets.
      {
        key: "anthropic-native", label: "Anthropic Claude (native alias)", host: "api.anthropic.com",
        protocolKind: "native", protocolLabel: "Native API",
        models: ["claude-opus-5", "claude-sonnet-5"], keyEnvVar: "ANTHROPIC_API_KEY",
        notes: "", warning: "", needsBaseUrl: false, local: false, recommended: true,
        profileName: "anthropic-native", connected: false
      },
      {
        key: "anthropic-compat", label: "Anthropic Claude compatibility", host: "api.anthropic.com",
        protocolKind: "compatibility", protocolLabel: "OpenAI-compatible",
        models: ["claude-sonnet-5"], keyEnvVar: "ANTHROPIC_API_KEY",
        notes: "", warning: "", needsBaseUrl: false, local: false, recommended: false,
        profileName: "anthropic-compat", connected: false
      },
      {
        key: "openai-responses", label: "OpenAI Responses", host: "api.openai.com",
        protocolKind: "native", protocolLabel: "Native API",
        models: ["gpt-5"], keyEnvVar: "OPENAI_API_KEY",
        notes: "", warning: "", needsBaseUrl: false, local: false, recommended: true,
        profileName: "openai-responses", connected: false
      },
      {
        key: "openai", label: "OpenAI", host: "api.openai.com",
        protocolKind: "compatibility", protocolLabel: "OpenAI-compatible",
        models: ["gpt-5"], keyEnvVar: "OPENAI_API_KEY",
        notes: "", warning: "", needsBaseUrl: false, local: false, recommended: false,
        profileName: "openai", connected: true
      },
      {
        key: "deepseek", label: "DeepSeek", host: "api.deepseek.com",
        protocolKind: "compatibility", protocolLabel: "OpenAI-compatible",
        models: ["deepseek-v4-pro"], keyEnvVar: "DEEPSEEK_API_KEY",
        notes: "", warning: "", needsBaseUrl: false, local: false, recommended: false,
        profileName: "deepseek", connected: false
      },
      {
        key: "groq", label: "Groq", host: "api.groq.com",
        protocolKind: "compatibility", protocolLabel: "OpenAI-compatible",
        models: ["llama-4-70b"], keyEnvVar: "GROQ_API_KEY",
        notes: "", warning: "", needsBaseUrl: false, local: false, recommended: false,
        profileName: "groq", connected: false
      },
      {
        key: "mistral", label: "Mistral AI", host: "api.mistral.ai",
        protocolKind: "compatibility", protocolLabel: "OpenAI-compatible",
        models: ["mistral-large"], keyEnvVar: "MISTRAL_API_KEY",
        notes: "", warning: "", needsBaseUrl: false, local: false, recommended: false,
        profileName: "mistral", connected: false
      },
      {
        key: "zhipu", label: "Zhipu / GLM", host: "open.bigmodel.cn",
        protocolKind: "compatibility", protocolLabel: "OpenAI-compatible",
        models: ["glm-5"], keyEnvVar: "ZHIPU_API_KEY",
        notes: "", warning: "", needsBaseUrl: false, local: false, recommended: false,
        profileName: "zhipu", connected: false
      },
      {
        key: "ollama", label: "Ollama (local)", host: "localhost:11434",
        protocolKind: "compatibility", protocolLabel: "OpenAI-compatible",
        models: ["llama4"], keyEnvVar: "",
        notes: "", warning: "", needsBaseUrl: false, local: true, recommended: false,
        profileName: "ollama", connected: false
      },
      {
        key: "custom", label: "Custom endpoint", host: "",
        protocolKind: "compatibility", protocolLabel: "OpenAI-compatible",
        models: [], keyEnvVar: "", notes: "Bring your own endpoint.",
        warning: "", needsBaseUrl: true, local: false, recommended: false,
        profileName: "custom", connected: false
      }
    ],
    connections: [
      {
        profile: "anthropic", host: "api.anthropic.com", baseUrl: "https://api.anthropic.com",
        model: "claude-sonnet-5", models: ["claude-sonnet-5"], protocolKind: "native",
        active: true, hasKey: true, keySource: "stored:profile=anthropic",
        keyEnvVar: "ANTHROPIC_API_KEY", storedInVsCode: true
      },
      {
        profile: "openai", host: "api.openai.com", baseUrl: "https://api.openai.com/v1",
        model: "gpt-5", models: ["gpt-5"], protocolKind: "compatibility",
        active: false, hasKey: false, keySource: "missing", keyEnvVar: "OPENAI_API_KEY",
        storedInVsCode: false
      }
    ]
  };
}

function postState(window: any, extra: Record<string, unknown> = {}): void {
  window.dispatchEvent(new window.MessageEvent("message", {
    data: {
      type: "state",
      state: {
        mode: "review",
        providerName: "anthropic",
        modelName: "claude-sonnet-5",
        engine: { tone: "ready", detail: "Connected" },
        workspace: { tone: "ready", detail: "Trusted" },
        provider: { tone: "ready", detail: "Configured" },
        conversation: { sessionId: null, mode: null, jobStatus: "idle", running: false, items: [] },
        recentTasks: [],
        commands: [],
        actionResults: [],
        runtimeEvents: [],
        models: modelsFixture(),
        ready: true,
        ...extra
      }
    }
  }));
}

function click(window: any, node: any): void {
  node.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
}

function text(node: any): string {
  return String(node?.textContent ?? "");
}

test("chat state updates preserve model options and provider form focus when the catalog is unchanged", () => {
  const { window, errors } = mountStartView();
  postState(window);
  const doc = window.document;
  const select = doc.querySelector(".connection-model-select");
  select.focus();
  postState(window, { conversation: { sessionId: "active", running: false, items: [{ id: "reply", kind: "assistant", text: "More text", status: "complete" }] } });
  assert.equal(doc.activeElement, select);
  doc.querySelector("#modelButton").click();
  const option = doc.querySelector("#modelList .command-row");
  postState(window);
  assert.equal(doc.querySelector("#modelList .command-row"), option);
  assert.equal(doc.querySelector("#modelPalette").hidden, false);
  assert.deepEqual(errors, []);
});

test("every connection is one card, the active one pinned first with a human name", () => {
  const { window } = mountStartView();
  postState(window);
  const document = window.document;

  const cards = [...document.querySelectorAll("#connectionsList .connection-card")];
  // Two sections, not three: the active connection is the head of one list rather than a card of
  // its own repeated in a "switch to" list below it.
  assert.equal(cards.length, 2);
  assert.equal(document.querySelector("#activeConnection"), null);
  assert.equal(document.querySelector("#configuredList"), null);

  // Display names come from the provider catalog; the profile slug is secondary text only.
  assert.deepEqual(
    cards.map((card: any) => text(card.querySelector(".connection-name"))),
    ["Anthropic Claude", "OpenAI"]
  );
  assert.equal(cards[0].classList.contains("active"), true);
  // The active tick is an inline SVG that follows the theme, not a raw glyph; the same fact still
  // reaches a screen reader as words in the adjacent .sr-only text.
  assert.equal(cards[0].querySelector("svg.connection-check")?.getAttribute("aria-hidden"), "true");
  assert.match(text(cards[0].querySelector(".sr-only")), /Active connection/);
  assert.equal(cards[1].querySelector(".connection-check"), null);
  assert.match(text(cards[0].querySelector(".connection-detail")), /^anthropic · api\.anthropic\.com/);

  // Identical anatomy on every card: name, chip, model dropdown, one primary action, overflow.
  for (const card of cards) {
    assert.notEqual(card.querySelector(".connection-chip"), null);
    assert.notEqual(card.querySelector(".connection-model-select"), null);
    assert.notEqual(card.querySelector(".connection-primary"), null);
    assert.notEqual(card.querySelector(".connection-more"), null);
  }

  // Health lives in the chip: quiet when it works, loud when it cannot run.
  assert.equal(text(cards[0].querySelector(".connection-chip")), "Connected");
  assert.equal(cards[0].querySelector(".connection-chip").classList.contains("warn"), false);
  assert.equal(text(cards[1].querySelector(".connection-chip")), "Needs a key");
  assert.equal(cards[1].querySelector(".connection-chip").classList.contains("warn"), true);
  assert.equal(cards[1].classList.contains("needs-key"), true);
});

test("a keyless active connection makes Add key the loudest control on the panel", () => {
  const { window, messages } = mountStartView();
  postState(window, {
    models: {
      ...modelsFixture(),
      connections: [
        {
          profile: "anthropic", host: "api.anthropic.com", baseUrl: "https://api.anthropic.com",
          model: "claude-sonnet-5", models: ["claude-sonnet-5"], protocolKind: "native",
          active: true, hasKey: false, keySource: "missing",
          keyEnvVar: "ANTHROPIC_API_KEY", storedInVsCode: false
        }
      ]
    }
  });
  const document = window.document;

  const card = document.querySelector("#connectionsList .connection-card");
  assert.equal(card.classList.contains("needs-key"), true);
  assert.equal(text(card.querySelector(".connection-chip")), "Needs a key");
  const primary = card.querySelector(".connection-primary");
  assert.equal(text(primary), "Add key");
  // "attention" is the only place the surface asks for the theme's primary button colours.
  assert.equal(primary.classList.contains("attention"), true);

  messages.length = 0;
  click(window, primary);
  assert.equal(
    JSON.stringify(messages),
    JSON.stringify([{ type: "provider.key", profile: "anthropic", keyAction: "update" }])
  );
});

test("the catalog merges transport twins into one provider and hides connected ones", () => {
  const { window } = mountStartView();
  postState(window);
  const document = window.document;

  const labels = [...document.querySelectorAll("#providerList .provider-label")].map((node: any) => text(node));
  // Anthropic and OpenAI are already connected, so they are not offered again; nothing anywhere
  // says "(native alias)", because an alias is a transport of a provider, not a provider.
  assert.deepEqual(labels, ["DeepSeek", "Groq", "Mistral AI", "Zhipu / GLM", "Ollama (local)"]);
  assert.doesNotMatch(text(document.querySelector("#modelsSurface")), /native alias/i);

  // At rest there is no count and no "Recommended" pill on every row — only the compatibility tag.
  assert.equal(text(document.querySelector("#catalogCount")), "");
  assert.equal(document.querySelectorAll("#providerList .provider-badge").length, 0);
  assert.deepEqual(
    [...document.querySelectorAll("#providerList .provider-tag")].map((node: any) => text(node)),
    ["OpenAI-compatible", "OpenAI-compatible", "OpenAI-compatible", "OpenAI-compatible", "OpenAI-compatible"]
  );

  // The long tail is one click away, and the count of it is honest.
  const toggle = document.querySelector("#catalogToggle");
  assert.equal(toggle.hidden, false);
  assert.equal(text(toggle), "Show all 6 providers");
  click(window, toggle);
  assert.equal(document.querySelectorAll("#providerList .provider-card").length, 6);
  // The control has to survive being used, or the list is stranded open.
  assert.equal(toggle.hidden, false);
  assert.equal(text(toggle), "Show fewer providers");
  assert.equal(toggle.getAttribute("aria-expanded"), "true");
  click(window, toggle);
  assert.equal(document.querySelectorAll("#providerList .provider-card").length, 5);
});

test("the short list spends at most one slot per organisation and never hides the rest", () => {
  const { window } = mountStartView();
  const models = modelsFixture();
  const regional = (region: string, index: number) => ({
    key: `qwen-${region}`, label: `Alibaba Qwen / DashScope (${region})`,
    host: `dashscope-${region}.aliyuncs.com`,
    protocolKind: "compatibility", protocolLabel: "OpenAI-compatible",
    models: [`qwen-${index}`], keyEnvVar: "DASHSCOPE_API_KEY",
    notes: "", warning: "", needsBaseUrl: false, local: false, recommended: false,
    profileName: `qwen-${region}`, connected: false
  });
  postState(window, {
    models: {
      ...models,
      providers: [
        regional("intl", 1),
        regional("us", 2),
        regional("cn", 3),
        ...(models.providers as any[]).filter((provider) => !provider.connected)
      ],
      connections: []
    }
  });
  const document = window.document;

  // One Qwen row, not three: regional endpoints of one vendor share a registrable domain, and
  // spending the whole short list on a single vendor is exactly the duplication being removed.
  const labels = [...document.querySelectorAll("#providerList .provider-label")].map((node: any) => text(node));
  assert.equal(labels.filter((label: string) => label.startsWith("Alibaba Qwen")).length, 1);
  assert.equal(labels.length, 5);

  // The two that were skipped are still reachable, and the advertised total is the real one.
  const toggle = document.querySelector("#catalogToggle");
  assert.equal(toggle.hidden, false);
  const announced = Number(/Show all (\d+) providers/.exec(text(toggle))?.[1]);
  click(window, toggle);
  const expanded = [...document.querySelectorAll("#providerList .provider-label")].map((node: any) => text(node));
  assert.equal(expanded.length, announced, "the expander delivers exactly what it advertised");
  assert.equal(expanded.filter((label: string) => label.startsWith("Alibaba Qwen")).length, 3);
  assert.equal(text(toggle), "Show fewer providers");
});

test("a provider with more than one transport picks the native one and offers the rest inline", () => {
  const { window, messages } = mountStartView();
  // Disconnect Anthropic so its family shows in the catalog with all three of its presets.
  const models = modelsFixture();
  postState(window, {
    models: {
      ...models,
      providers: (models.providers as any[]).map((provider) => ({ ...provider, connected: false })),
      connections: []
    }
  });
  const document = window.document;

  const labels = [...document.querySelectorAll("#providerList .provider-label")].map((node: any) => text(node));
  assert.deepEqual(labels.slice(0, 2), ["Anthropic Claude", "OpenAI"]);
  // The native transport is the default, so the merged row carries no compatibility tag.
  assert.equal(document.querySelector("#providerList .provider-card:first-child .provider-tag"), null);

  click(window, document.querySelector("#providerList .provider-head"));
  const transport = document.querySelector("#providerList .provider-form select");
  assert.deepEqual(
    [...transport.options].map((option: any) => option.textContent),
    ["Native API", "OpenAI-compatible"]
  );
  // Two native presets for one endpoint are the same choice twice, so only one is offered.
  assert.match(text(document.querySelector(".provider-transport-note")), /Anthropic Claude preset/);

  messages.length = 0;
  click(window, document.querySelector("#providerList .provider-form .primary-action"));
  assert.equal((messages[0] as any).presetKey, "anthropic");

  transport.selectedIndex = 1;
  transport.dispatchEvent(new window.Event("change", { bubbles: true }));
  assert.match(text(document.querySelector(".provider-transport-note")), /Anthropic Claude compatibility preset/);
  messages.length = 0;
  click(window, document.querySelector("#providerList .provider-form .primary-action"));
  assert.equal((messages[0] as any).presetKey, "anthropic-compat");
});

test("connecting a provider sends the model shown in the form, including a typed one", () => {
  const { window, messages } = mountStartView();
  postState(window);
  const document = window.document;

  click(window, document.querySelectorAll("#providerList .provider-head")[0]);
  assert.equal(document.querySelectorAll("#providerList .provider-form").length, 1);
  // Keys are collected by a native VS Code prompt, so no key field exists in the webview at all.
  assert.equal(document.querySelector("#providerList .provider-form input[type=password]"), null);
  assert.match(text(document.querySelector("#providerList .provider-key-note")), /stored securely in VS Code/);

  messages.length = 0;
  click(window, document.querySelector("#providerList .provider-form .primary-action"));
  assert.equal(
    JSON.stringify(messages[0]),
    JSON.stringify({ type: "provider.connect", presetKey: "deepseek", model: "deepseek-v4-pro", baseUrl: "" })
  );

  // Choosing "Other" stays in free-text mode across re-renders and sends exactly what was typed.
  const select = document.querySelector("#providerList .provider-form select");
  select.selectedIndex = select.options.length - 1;
  select.dispatchEvent(new window.Event("change", { bubbles: true }));
  const typed = document.querySelector("#providerList .provider-form input[type=text]");
  assert.notEqual(typed, null, "choosing Other reveals a free-text model field");
  typed.value = "deepseek-experimental";
  typed.dispatchEvent(new window.Event("input", { bubbles: true }));
  assert.notEqual(
    document.querySelector("#providerList .provider-form input[type=text]"),
    null,
    "the field does not snap back to the dropdown on the next render"
  );

  messages.length = 0;
  click(window, document.querySelector("#providerList .provider-form .primary-action"));
  assert.equal(
    JSON.stringify(messages[0]),
    JSON.stringify({ type: "provider.connect", presetKey: "deepseek", model: "deepseek-experimental", baseUrl: "" })
  );
});

test("a provider with no preset endpoint asks for a base URL", () => {
  const { window } = mountStartView();
  postState(window);
  const document = window.document;

  // The custom endpoint sorts last, so it lives behind the "show all" expander.
  click(window, document.querySelector("#catalogToggle"));
  const heads = [...document.querySelectorAll("#providerList .provider-head")];
  click(window, heads[heads.length - 1]);
  const labels = [...document.querySelectorAll("#providerList .provider-field-label")].map((node: any) => text(node));
  assert.deepEqual(labels, ["Base URL", "Model"]);
});

test("a busy provider card narrates each connect phase instead of silently disabling", () => {
  const { window } = mountStartView();
  postState(window);
  const document = window.document;

  // Expand deepseek, then report it busy waiting for the key: the native prompt opens at the top
  // of the window, so the card the user actually clicked must say where to look.
  click(window, document.querySelectorAll("#providerList .provider-head")[0]);
  postState(window, { models: { ...modelsFixture(), busy: "deepseek", busyPhase: "waiting_for_key" } });

  const form = document.querySelector("#providerList .provider-form");
  assert.match(text(form.querySelector(".provider-busy-phase")), /asking for your API key/i);
  assert.equal(form.querySelector(".primary-action").disabled, true);
  assert.equal(text(form.querySelector(".primary-action")), "Working...");

  postState(window, { models: { ...modelsFixture(), busy: "deepseek", busyPhase: "checking_key" } });
  assert.match(text(document.querySelector("#providerList .provider-busy-phase")), /checking your key/i);

  // The narration clears with the busy state.
  postState(window, { models: modelsFixture() });
  assert.equal(document.querySelector("#providerList .provider-busy-phase"), null);
  assert.equal(document.querySelector("#providerList .provider-form .primary-action").disabled, false);
});

test("provider search shows a match count and every match, and leaves connections alone", () => {
  const { window, errors } = mountStartView();
  postState(window);
  const document = window.document;

  const search = document.querySelector("#providerSearch");
  search.value = "deep";
  search.dispatchEvent(new window.Event("input", { bubbles: true }));

  assert.equal(document.querySelectorAll("#providerList .provider-card").length, 1);
  // A count belongs to a filter, not to the surface at rest.
  assert.equal(text(document.querySelector("#catalogCount")), "1 match");
  // Searching reaches past the short list, so the expander steps aside while a query is active.
  assert.equal(document.querySelector("#catalogToggle").hidden, true);
  assert.equal(document.querySelectorAll("#connectionsList .connection-card").length, 2);

  search.value = "";
  search.dispatchEvent(new window.Event("input", { bubbles: true }));
  assert.equal(text(document.querySelector("#catalogCount")), "");

  // A merged provider stays findable by any of the presets behind it.
  search.value = "native alias";
  search.dispatchEvent(new window.Event("input", { bubbles: true }));
  assert.equal(text(document.querySelector("#catalogCount")), "0 matches", "Anthropic is connected, so it is not offered");
  search.value = "responses";
  search.dispatchEvent(new window.Event("input", { bubbles: true }));
  assert.equal(text(document.querySelector("#catalogCount")), "0 matches", "OpenAI is connected, so it is not offered");
  assert.deepEqual(errors, [], "a zero-result provider search must not interrupt the webview");
});

test("a connection card routes switching and key management without exposing key values", () => {
  const { window, messages } = mountStartView();
  postState(window);
  const document = window.document;

  const openAiCard = document.querySelectorAll("#connectionsList .connection-card")[1];
  const primary = openAiCard.querySelector(".connection-primary");
  assert.equal(text(primary), "Add key", "a keyless connection asks for the key before offering the switch");

  // The active connection's primary slot is a state, not an action.
  const activePrimary = document.querySelector("#connectionsList .connection-card .connection-primary");
  assert.equal(text(activePrimary), "In use");
  assert.equal(activePrimary.disabled, true);

  messages.length = 0;
  click(window, primary);
  assert.equal(
    JSON.stringify(messages),
    JSON.stringify([{ type: "provider.key", profile: "openai", keyAction: "update" }])
  );

  // Replace key / Remove key live in the overflow so the card keeps exactly one primary action.
  assert.equal(document.querySelector(".connection-menu"), null);
  click(window, document.querySelector("#connectionsList .connection-card .connection-more"));
  const items = [...document.querySelectorAll(".connection-menu-item")].map((node: any) => text(node));
  assert.deepEqual(items, ["Replace key", "Remove key", "Reconnect..."]);

  messages.length = 0;
  click(window, [...document.querySelectorAll(".connection-menu-item")][1]);
  assert.equal(
    JSON.stringify(messages),
    JSON.stringify([{ type: "provider.key", profile: "anthropic", keyAction: "forget" }])
  );
  assert.equal(document.querySelector(".connection-menu"), null, "acting closes the menu");
});

test("only the active connection's model dropdown can change the model", () => {
  const { window, messages } = mountStartView();
  postState(window);
  const document = window.document;

  const selects = [...document.querySelectorAll("#connectionsList .connection-model-select")];
  assert.equal(selects.length, 2);
  assert.deepEqual(
    [...selects[0].options].map((option: any) => option.value),
    ["claude-sonnet-5", "claude-opus-5"]
  );
  assert.equal(selects[0].disabled, false);
  assert.equal(selects[1].disabled, true, "an inactive connection's model is read-only");

  messages.length = 0;
  selects[0].selectedIndex = 1;
  selects[0].dispatchEvent(new window.Event("change", { bubbles: true }));
  assert.equal(
    JSON.stringify(messages),
    JSON.stringify([{ type: "model.set", model: "claude-opus-5" }])
  );
});

test("the composer model picker switches model and provider without leaving the chat", () => {
  const { window, messages } = mountStartView();
  postState(window);
  const document = window.document;

  click(window, document.querySelector("#modelButton"));
  assert.equal(document.querySelector("#modelPalette").hidden, false);
  assert.equal(document.querySelector("#modelButton").getAttribute("aria-expanded"), "true");

  // One naming system: the picker names providers the same way the Models surface does.
  const titles = [...document.querySelectorAll("#modelList .command-row .command-title")]
    .map((node: any) => text(node));
  assert.deepEqual(titles, ["claude-sonnet-5", "claude-opus-5", "OpenAI"]);
  const sections = [...document.querySelectorAll("#modelList .subsection-label")].map((node: any) => text(node));
  assert.deepEqual(sections, ["Models · Anthropic Claude", "Switch provider"]);

  messages.length = 0;
  click(window, document.querySelectorAll("#modelList .command-row")[1]);
  assert.equal(
    JSON.stringify(messages.filter((message: any) => message.type === "model.set")),
    JSON.stringify([{ type: "model.set", model: "claude-opus-5" }])
  );
  assert.equal(document.querySelector("#modelPalette").hidden, true, "picking a model closes the popover");

  click(window, document.querySelector("#modelButton"));
  messages.length = 0;
  click(window, [...document.querySelectorAll("#modelList .command-row")].at(-1));
  assert.equal(
    JSON.stringify(messages.filter((message: any) => message.type === "provider.use")),
    JSON.stringify([{ type: "provider.use", profile: "openai" }])
  );
});

test("the model picker asks the host for the catalog the first time it opens", () => {
  const { window, messages } = mountStartView();
  const document = window.document;

  click(window, document.querySelector("#modelButton"));
  assert.equal(
    JSON.stringify(messages.filter((message: any) => message.type === "models.refresh")),
    JSON.stringify([{ type: "models.refresh" }])
  );
  assert.match(text(document.querySelector("#modelList")), /Loading providers/);
});

test("the composer @ picker searches the workspace and inserts the chosen path", async () => {
  const { window, messages } = mountStartView();
  postState(window);
  const document = window.document;
  const input = document.querySelector("#taskInput");

  input.value = "look at @app";
  input.selectionStart = input.value.length;
  input.selectionEnd = input.value.length;
  messages.length = 0;
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 200));

  const search = messages.filter((message: any) => message.type === "mention.search");
  assert.equal(search.length, 1, "keystrokes are debounced into a single workspace search");
  assert.equal(search[0].query, "app");

  window.dispatchEvent(new window.MessageEvent("message", {
    data: {
      type: "mention.results",
      token: search[0].token,
      results: [
        { label: "app.ts", detail: "src/app.ts", insert: "src/app.ts", kind: "file" },
        { label: "app.css", detail: "media/app.css", insert: "media/app.css", kind: "file" }
      ]
    }
  }));
  assert.equal(document.querySelector("#mentionPalette").hidden, false);
  assert.equal(document.querySelectorAll("#mentionList .command-row").length, 2);

  click(window, document.querySelectorAll("#mentionList .command-row")[0]);
  assert.equal(input.value, "look at @src/app.ts ");
  assert.equal(document.querySelector("#mentionPalette").hidden, true);
});

test("mention answers for superseded keystrokes are dropped", async () => {
  const { window, messages } = mountStartView();
  postState(window);
  const document = window.document;
  const input = document.querySelector("#taskInput");

  input.value = "@a";
  input.selectionStart = 2;
  input.selectionEnd = 2;
  messages.length = 0;
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 200));
  const token = messages.filter((message: any) => message.type === "mention.search")[0].token;

  // A slower search for an older keystroke must not repaint the list behind the current one.
  window.dispatchEvent(new window.MessageEvent("message", {
    data: {
      type: "mention.results",
      token: token - 1,
      results: [{ label: "stale.ts", detail: "stale.ts", insert: "stale.ts", kind: "file" }]
    }
  }));
  assert.equal(document.querySelector("#mentionPalette").hidden, true);
});

test("a slash command takes the composer back from the @ picker", async () => {
  const { window } = mountStartView();
  postState(window, {
    commands: [
      {
        id: "c1",
        command: "alysis.showHistory",
        title: "Task History",
        description: "",
        category: "Alysis Code",
        mutates: false,
        available: true,
        unavailableReason: ""
      }
    ]
  });
  const document = window.document;
  const input = document.querySelector("#taskInput");

  input.value = "@a";
  input.selectionStart = 2;
  input.selectionEnd = 2;
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 200));
  window.dispatchEvent(new window.MessageEvent("message", {
    data: {
      type: "mention.results",
      token: 1,
      results: [{ label: "a.ts", detail: "a.ts", insert: "a.ts", kind: "file" }]
    }
  }));

  input.value = "/hist";
  input.selectionStart = 5;
  input.selectionEnd = 5;
  input.dispatchEvent(new window.Event("input", { bubbles: true }));

  assert.equal(document.querySelector("#commandPalette").hidden, false);
  assert.equal(document.querySelector("#mentionPalette").hidden, true);
});

test("the start surface offers setup until a model is connected", () => {
  const { window, messages } = mountStartView();
  const document = window.document;
  const input = document.querySelector("#taskInput");
  const send = document.querySelector("#submitTask");

  postState(window, { ready: false });
  assert.equal(document.querySelector("#connectFirst").hidden, false);
  assert.equal(document.querySelector("#starterPrompts").hidden, true);
  assert.equal(input.placeholder, "Finish setup to start…");
  input.value = "Do not dispatch before setup";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  assert.equal(send.disabled, true);
  input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  assert.equal(messages.some((message: any) => message.type === "task.submit"), false);

  postState(window, { ready: true });
  assert.equal(document.querySelector("#connectFirst").hidden, true);
  assert.equal(document.querySelector("#starterPrompts").hidden, false);
  assert.equal(input.placeholder, "Ask Alysis Code…");
  assert.equal(send.disabled, false);
});

test("Start Here fails closed when a host state carries no provider readiness data", () => {
  const { window } = mountStartView();
  const document = window.document;

  // Missing readiness cannot be interpreted as a valid provider/model selection.
  window.dispatchEvent(new window.MessageEvent("message", {
    data: {
      type: "state",
      state: {
        mode: "review",
        engine: { tone: "ready", detail: "" },
        workspace: { tone: "ready", detail: "" },
        provider: { tone: "ready", detail: "" },
        conversation: { sessionId: null, mode: null, jobStatus: "idle", running: false, items: [] }
      }
    }
  }));

  assert.equal(document.querySelector("#connectFirst").hidden, false);
  assert.equal(document.querySelector("#taskInput").placeholder, "Finish setup to start…");
  assert.match(text(document.querySelector("#connectionsList")), /Loading your connections/);
});

test("the '/' menu lists real slash commands, inserts the pick, and closes once arguments are typed", () => {
  const { window, messages } = mountStartView();
  postState(window, {
    slashCommands: [
      { command: "/model", title: "Model", description: "Switch the model for this task.", usage: "/model <name>", takesArgs: true },
      { command: "/doctor", title: "Doctor", description: "Check the local setup.", usage: "/doctor", takesArgs: false },
      { command: "/compact", title: "Compact", description: "Summarize earlier context.", usage: "/compact", takesArgs: false }
    ]
  });
  const document = window.document;
  const input = document.querySelector("#taskInput");
  const palette = document.querySelector("#commandPalette");

  input.value = "/";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  assert.equal(palette.hidden, false);
  const rows = Array.from(document.querySelectorAll("#commandList .command-row")).map((row: any) => text(row.querySelector("code")));
  assert.deepEqual(rows, ["/compact", "/doctor", "/model"]);

  input.value = "/mo";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  assert.equal(document.querySelectorAll("#commandList .command-row").length, 1);
  click(window, document.querySelector("#commandList .command-row"));
  assert.equal(input.value, "/model ", "an argument-taking command is inserted with a trailing space");
  assert.equal(palette.hidden, true);

  input.value = "/model my-local-model";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  assert.equal(palette.hidden, true, "typing arguments must not reopen the menu");
  input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  const submit = messages.find((message: any) => message.type === "task.submit");
  assert.ok(submit, "Enter sends the slash command with its arguments");
  assert.equal(submit.instruction, "/model my-local-model");
});

test("picking an argument-less slash command sends it immediately", () => {
  const { window, messages } = mountStartView();
  postState(window, {
    slashCommands: [
      { command: "/doctor", title: "Doctor", description: "Check the local setup.", usage: "/doctor", takesArgs: false }
    ]
  });
  const document = window.document;
  const input = document.querySelector("#taskInput");
  input.value = "/doc";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  click(window, document.querySelector("#commandList .command-row"));
  const submit = messages.find((message: any) => message.type === "task.submit");
  assert.ok(submit);
  assert.equal(submit.instruction, "/doctor");
});

test("a sent message shows in the transcript immediately, then yields to the host's copy", () => {
  const { window } = mountStartView();
  postState(window);
  const document = window.document;
  const input = document.querySelector("#taskInput");

  input.value = "Explain this repo";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));

  assert.equal(document.querySelector("#welcome").hidden, true);
  const pending = document.querySelector(".message.user.pending");
  assert.ok(pending, "the user's message is visible before the host acknowledges it");
  assert.equal(text(pending).includes("Explain this repo"), true);
  assert.ok(document.querySelector(".work-progress-thinking"), "a starting indicator accompanies it");
  assert.equal(input.value, "");

  // The host publishes its own copy of the message: the optimistic one must disappear.
  postState(window, {
    conversation: {
      sessionId: "s1", mode: "review", jobStatus: "running", running: true,
      items: [{ id: "user:1", kind: "user", title: "You", text: "Explain this repo", status: "", toolName: null, toolInput: null, semanticActivity: false, approvalId: null, allowForSession: false, errorKind: "", errorTitle: "", errorDetail: "", errorActions: [], retryAfterSeconds: null }]
    }
  });
  assert.equal(document.querySelectorAll(".message.user").length, 1);
  assert.equal(document.querySelector(".message.user.pending"), null);
});

test("the start hero offers the blocker's own recovery actions, not a generic provider button", () => {
  const { window, messages } = mountStartView();
  postState(window, {
    ready: false,
    readiness: {
      ok: false,
      blockers: [
        { id: "runtime_missing", severity: "error", title: "Locate the CLI on this computer", detail: "Alysis Code could not find its local engine.", actions: [{ label: "Locate the CLI", command: "alysis.locateCli", primary: true }, { label: "Open setup guide", command: "alysis.openSetupGuide" }] },
        { id: "workspace_untrusted", severity: "warning", title: "Limited until you trust this folder", detail: "", actions: [] }
      ]
    }
  });
  const document = window.document;
  assert.equal(text(document.querySelector("#connectFirstTitle")), "Locate the CLI on this computer");
  const buttons = Array.from(document.querySelectorAll("#connectFirstActions button")).map((button: any) => text(button));
  assert.deepEqual(buttons, ["Locate the CLI", "Open setup guide"]);
  click(window, document.querySelector("#connectFirstActions button"));
  assert.ok(messages.some((message: any) => message.type === "command" && message.command === "alysis.locateCli"));
  // The hero already explains the primary blocker; the strip carries only the remaining one.
  const stripIds = Array.from(document.querySelectorAll("#readinessStrip .readiness-row")).map((row: any) => row.dataset.blockerId);
  assert.deepEqual(stripIds, ["workspace_untrusted"]);
});

test("a still-running provider check renders as progress with no buttons", () => {
  const { window } = mountStartView();
  postState(window, {
    ready: false,
    readiness: { ok: false, blockers: [{ id: "provider_loading", severity: "warning", title: "Checking your AI connection…", detail: "", actions: [], progress: true }] }
  });
  const document = window.document;
  assert.ok(document.querySelector("#connectFirstTitle .progress-spinner"));
  assert.equal(document.querySelectorAll("#connectFirstActions button").length, 0);
  assert.equal(document.querySelector("#taskInput").placeholder, "Checking your connection…");
});
