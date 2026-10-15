import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import Module from "node:module";
import { resolve } from "node:path";
import test from "node:test";

import { SENSITIVE_ALYSIS_SETTINGS } from "../src/client/CliDiscovery";
import { COMMANDS } from "../src/commands/registry";

const ALL_COMMANDS: readonly string[] = Object.values(COMMANDS);

test("package.json contributes every registered command id", () => {
  const packageRoot = resolve(__dirname, "../..");
  const packageJson = JSON.parse(readFileSync(resolve(packageRoot, "package.json"), "utf8")) as {
    contributes: { commands: Array<{ command: string }> };
  };
  const contributed = new Set(packageJson.contributes.commands.map((command) => command.command));

  assert.deepEqual(new Set(ALL_COMMANDS), contributed);
});

test("structured review and Source Control have distinct public command contracts and production wiring", () => {
  const packageRoot = resolve(__dirname, "../..");
  const manifest = JSON.parse(readFileSync(resolve(packageRoot, "package.json"), "utf8"));
  for (const [command, title] of [[COMMANDS.reviewGitChanges, "Review Git Changes"], [COMMANDS.openSourceControl, "Open Source Control"]]) {
    assert.deepEqual(manifest.contributes.commands.filter((entry: any) => entry.command === command), [{ command, title, category: "Alysis Code" }]);
    assert.equal(manifest.contributes.menus.commandPalette.some((entry: any) => entry.command === command && entry.when === "false"), false);
  }
  const extension = readFileSync(resolve(packageRoot, "src/extension.ts"), "utf8");
  assert.match(extension, /const reviewGitChanges = registerReviewCommands\(context, backendActions\)/);
  assert.match(extension, /worktreeAction: \(action\) => action === "review" \? reviewGitChanges\(\)/);
  assert.doesNotMatch(extension, /worktrees\.review\(/);
});

test("extension activation stays lazy while every user entry point can activate it", () => {
  const packageRoot = resolve(__dirname, "../..");
  const packageJson = JSON.parse(readFileSync(resolve(packageRoot, "package.json"), "utf8")) as {
    activationEvents?: string[];
    contributes: {
      commands: Array<{ command: string }>;
      views: Record<string, Array<{ id: string }>>;
      chatParticipants?: Array<{ id: string }>;
    };
  };
  const activationEvents = new Set(packageJson.activationEvents ?? []);

  assert.equal(activationEvents.has("onStartupFinished"), false, "opening VS Code must not eagerly start Alysis Code");
  assert.equal(activationEvents.has("*"), false, "Alysis Code must never activate unconditionally");

  // VS Code >= 1.74 (this extension requires >= 1.90) derives an activation event from every
  // contributed command, view, and chat participant. Repeating them in `activationEvents` adds
  // nothing and buries the one entry that matters, so the manifest declares only the internal
  // command that has no contribution to derive an event from.
  for (const command of packageJson.contributes.commands) {
    assert.equal(
      activationEvents.has(`onCommand:${command.command}`),
      false,
      `${command.command} is contributed and therefore activates implicitly`
    );
  }
  for (const view of packageJson.contributes.views.alysis ?? []) {
    assert.equal(activationEvents.has(`onView:${view.id}`), false, `${view.id} activates implicitly`);
  }
  for (const participant of packageJson.contributes.chatParticipants ?? []) {
    assert.equal(
      activationEvents.has(`onChatParticipant:${participant.id}`),
      false,
      `${participant.id} activates implicitly`
    );
  }
  assert.deepEqual(
    [...activationEvents],
    ["onCommand:alysis.runtimeEvidence"],
    "only non-contributed commands need an explicit activation event"
  );
});

test("package.json command titles do not duplicate the Alysis Code category prefix", () => {
  const packageRoot = resolve(__dirname, "../..");
  const packageJson = JSON.parse(readFileSync(resolve(packageRoot, "package.json"), "utf8")) as {
    contributes: { commands: Array<{ command: string; title: string; category?: string }> };
  };

  for (const command of packageJson.contributes.commands) {
    if (command.category === "Alysis Code") {
      assert.equal(command.title.startsWith("Alysis Code:"), false, command.command);
    }
  }
});

test("package.json contributes cockpit entry points and a getting-started walkthrough", () => {
  const packageRoot = resolve(__dirname, "../..");
  const packageJson = JSON.parse(readFileSync(resolve(packageRoot, "package.json"), "utf8")) as {
    contributes: {
      commands: Array<{ command: string; title: string; category?: string }>;
      viewsWelcome?: Array<{ view: string; contents: string }>;
      views?: Record<string, Array<{ id: string; name?: string; type?: string; visibility?: string; when?: string }>>;
      walkthroughs?: Array<{ id: string; title: string; steps: Array<{ id: string; media?: { markdown?: string } }> }>;
      menus?: { "view/title"?: Array<{ command: string; when?: string; group?: string }> };
    };
  };
  const contributes = packageJson.contributes;

  // alysis.openChat is the cockpit entry point: clear category + non-empty title.
  const openChat = contributes.commands.find((command) => command.command === "alysis.openChat");
  assert.equal(openChat?.category, "Alysis Code");
  assert.ok((openChat?.title ?? "").length > 0);
  // The Locate CLI command is contributed.
  assert.ok(contributes.commands.some((command) => command.command === "alysis.locateCli"));

  // The single webview owns the complete sidebar. The legacy tree views were contributed but
  // permanently gated `when: "false"`, so they could never render — along with their menus and the
  // viewsWelcome bound to them. They are gone; the providers behind them survive as data sources
  // for the webview, not as UI.
  assert.equal(contributes.viewsWelcome, undefined, "no viewsWelcome can attach to a webview view");

  const views = contributes.views?.alysis ?? [];
  assert.deepEqual(
    views.map(({ id, name }) => ({ id, name })),
    [{ id: "alysis.start", name: "Alysis Code" }]
  );
  const startView = views.find((view) => view.id === "alysis.start");
  assert.equal(startView?.type, "webview");
  assert.equal(startView?.visibility, "visible");
  for (const id of ["alysis.recentWork", "alysis.plan", "alysis.results", "alysis.connections"]) {
    assert.equal(views.some((view) => view.id === id), false, `${id} is no longer contributed`);
  }

  // Match the compact agent-extension convention: New, capabilities, history, account, settings.
  const viewTitle = contributes.menus?.["view/title"] ?? [];
  assert.equal(viewTitle.some((entry) => entry.command === "alysis.openChat"), false);
  assert.deepEqual(
    viewTitle
      .filter((entry) => entry.when === "view == alysis.start")
      .map((entry) => entry.command),
    [
      "alysis.newSession",
      "alysis.showForge",
      "alysis.showBrowser",
      "alysis.showHistory",
      "alysis.configureProvider",
      "alysis.showSettings"
    ]
  );

  // The Get Started walkthrough exists with the expected steps + packaged markdown.
  const walkthrough = (contributes.walkthroughs ?? []).find((entry) => entry.id === "alysis.gettingStarted");
  assert.ok(walkthrough, "alysis.gettingStarted walkthrough");
  const stepIds = new Set(walkthrough!.steps.map((step) => step.id));
  for (const id of ["installCli", "trustWorkspace", "configureProvider", "openCockpit"]) {
    assert.ok(stepIds.has(id), `walkthrough step ${id}`);
  }
  for (const step of walkthrough!.steps) {
    const markdown = step.media?.markdown;
    assert.ok(markdown && existsSync(resolve(packageRoot, markdown)), `walkthrough markdown exists: ${markdown}`);
  }
});

test("Start Here webview keeps its restrained palette and handles UI state safely", () => {
  const packageRoot = resolve(__dirname, "../..");
  const css = readFileSync(resolve(packageRoot, "media/startView.css"), "utf8");
  const script = readFileSync(resolve(packageRoot, "media/startView.js"), "utf8");
  const provider = readFileSync(resolve(packageRoot, "src/views/StartViewProvider.ts"), "utf8")
    + readFileSync(resolve(packageRoot, "media/startView.html"), "utf8");

  assert.match(css, /--alysis-green:\s*var\(--vscode-button-background/);
  assert.doesNotMatch(css, /#9d96ff|#6f68d9|#b8b3ff|#756ee0/i);
  assert.doesNotMatch(css, /amber|#f2b84b|#ffca68|#d8a94f/i);
  assert.match(css, /--vscode-textLink-foreground/);
  assert.doesNotMatch(script, /innerHTML/);
  assert.match(provider, /<textarea id="taskInput"/);
  assert.doesNotMatch(provider, /<details class="readiness">/);
  assert.match(provider, /id="permissionStrip"/);
  assert.doesNotMatch(provider, /id="planMode"/);
  assert.doesNotMatch(provider, /id="actMode"/);
  assert.match(provider, /COMMANDS\.showBrowser/);
  assert.match(provider, /type: "task\.submit"/);
  assert.match(provider, /id="conversationItems"/);
  assert.match(script, /renderConversationItem/);
  assert.match(script, /type: "approval"/);
  assert.match(css, /\.agent-shell[\s\S]*height:\s*100vh/);
  assert.match(css, /\.surface-stack[\s\S]*overflow:\s*hidden/);
  assert.match(css, /\.task-composer[\s\S]*flex:\s*0 0 auto/);
  assert.match(script, /host\.getState\(\)/);
  assert.match(script, /host\.setState\(/);
  assert.match(script, /taskPending/);
  assert.match(provider, /type: "task\.result"/);
  assert.match(script, /event\.key === "Enter" && !event\.shiftKey/);
  assert.match(provider, /Not checked yet/);
  assert.match(provider, /default-src 'none'/);
  assert.match(provider, /isStartViewMessage\(message\)/);
});

test("package.json restricts sensitive Alysis Code settings in untrusted workspaces", () => {
  const packageRoot = resolve(__dirname, "../..");
  const packageJson = JSON.parse(readFileSync(resolve(packageRoot, "package.json"), "utf8")) as {
    capabilities: {
      untrustedWorkspaces: {
        restrictedConfigurations?: string[];
      };
    };
  };
  const restricted = new Set(packageJson.capabilities.untrustedWorkspaces.restrictedConfigurations ?? []);

  for (const setting of SENSITIVE_ALYSIS_SETTINGS) {
    assert.equal(restricted.has(`alysis.${setting}`), true, setting);
  }
});

test("CLI path is machine-specific while still permitting an explicit workspace override", () => {
  const packageRoot = resolve(__dirname, "../..");
  const packageJson = JSON.parse(readFileSync(resolve(packageRoot, "package.json"), "utf8")) as {
    contributes: { configuration: { properties: Record<string, { scope?: string }> } };
  };

  // `machine` — not `machine-overridable`. A workspace-settable executable path lets a cloned
  // repository nominate its own binary, which the trusted branch of resolveCliPathSecurity then
  // spawns on activation with credential forwarding enabled. Trusting a repo's authors is not
  // consent to run a binary the repo chose.
  assert.equal(
    packageJson.contributes.configuration.properties["alysis.cliPath"]?.scope,
    "machine"
  );
});

test("provider base URL is machine-specific so a repository cannot redirect the stored API key", () => {
  const packageRoot = resolve(__dirname, "../..");
  const packageJson = JSON.parse(readFileSync(resolve(packageRoot, "package.json"), "utf8")) as {
    contributes: { configuration: { properties: Record<string, { scope?: string }> } };
  };

  // Same argument as cliPath above. ensureCredentialBridge forwards the SecretStorage API
  // key to whatever host baseUrl names; with `machine-overridable`, a trusted repository's
  // .vscode/settings.json could name that host. Trusting a repo's authors is not consent
  // to send an API key to a host the repo chose.
  assert.equal(
    packageJson.contributes.configuration.properties["alysis.baseUrl"]?.scope,
    "machine"
  );
});

test("package.json rejects virtual workspaces that cannot provide local CLI and file paths", () => {
  const packageRoot = resolve(__dirname, "../..");
  const packageJson = JSON.parse(readFileSync(resolve(packageRoot, "package.json"), "utf8")) as {
    capabilities: { virtualWorkspaces?: { supported?: boolean; description?: string } };
  };
  const capability = packageJson.capabilities.virtualWorkspaces;

  assert.equal(capability?.supported, false);
  assert.match(capability?.description ?? "", /local workspace file paths/);
  assert.match(capability?.description ?? "", /local executable CLI/);
});

test("package.json has Marketplace-ready metadata without publication claims", () => {
  const packageRoot = resolve(__dirname, "../..");
  const packageJson = JSON.parse(readFileSync(resolve(packageRoot, "package.json"), "utf8")) as {
    version: string;
    repository?: { type?: string; url?: string; directory?: string };
    icon?: string;
    keywords?: string[];
    galleryBanner?: { color?: string; theme?: string };
    pricing?: string;
    preview?: boolean;
    bugs?: { url?: string };
    categories?: string[];
  };

  assert.match(packageJson.version, /^\d+\.\d+\.\d+$/);
  assert.notEqual(packageJson.version, "0.0.1");
  assert.deepEqual(packageJson.repository, {
    type: "git",
    url: "https://github.com/AlysisAi/AlysisCode.git",
    directory: "extensions/vscode-alysis"
  });
  assert.equal(packageJson.icon, "resources/icon.png");
  assert.equal(existsSync(resolve(packageRoot, packageJson.icon)), true);
  assert.deepEqual(readFileSync(resolve(packageRoot, packageJson.icon)).subarray(0, 8), Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));
  const vscodeIgnore = readFileSync(resolve(packageRoot, ".vscodeignore"), "utf8");
  // Individual unused files may be excluded, but the directories the runtime loads from must ship.
  assert.doesNotMatch(vscodeIgnore, /^resources\/(\*\*|$)/m);
  assert.doesNotMatch(vscodeIgnore, /^media\/(\*\*|$)/m);
  assert.equal(packageJson.keywords?.includes("coding-agent"), true);
  assert.equal(packageJson.galleryBanner?.theme, "dark");

  // The listing page and the internal release runbook are different documents. Shipping the latter
  // leaks protected environment and secret names to every installer.
  assert.match(vscodeIgnore, /^RELEASE_CHECKLIST\.md$/m);
  assert.match(vscodeIgnore, /^docs\/\*\*$/m);

  // Marketplace surfaces that decide whether a developer installs at all.
  assert.equal(packageJson.pricing, "Free");
  assert.equal(packageJson.preview, false);
  assert.equal(packageJson.bugs?.url, "https://github.com/AlysisAi/AlysisCode/issues");
  assert.ok((packageJson.categories ?? []).includes("AI"), "AI is where coding agents are browsed");
  assert.ok((packageJson.categories ?? []).includes("Chat"));

  // The README is the listing page; it must not disclaim its own publication.
  const readme = readFileSync(resolve(packageRoot, "README.md"), "utf8");
  assert.doesNotMatch(readme, /not published to the Marketplace/i);
  assert.doesNotMatch(readme, /scaffold/i);
});

test("Activity Bar and webviews use the packaged Alysis Code brand assets", () => {
  const packageRoot = resolve(__dirname, "../..");
  const packageJson = JSON.parse(readFileSync(resolve(packageRoot, "package.json"), "utf8")) as {
    contributes: { viewsContainers: { activitybar: Array<{ id: string; icon: string }> } };
  };
  const container = packageJson.contributes.viewsContainers.activitybar.find((item) => item.id === "alysis");
  assert.equal(container?.icon, "resources/alysis-logo.svg");
  assert.equal(existsSync(resolve(packageRoot, "resources/alysis-logo.svg")), true);
  assert.equal(existsSync(resolve(packageRoot, "resources/alysis-logo.png")), true);
});

test("package.json keeps VSIX packaging strict and publish scripts non-mutating", () => {
  const packageRoot = resolve(__dirname, "../..");
  const packageJson = JSON.parse(readFileSync(resolve(packageRoot, "package.json"), "utf8")) as {
    scripts: Record<string, string>;
  };
  const scripts = packageJson.scripts;
  const scriptText = Object.entries(scripts)
    .map(([name, script]) => `${name}: ${script}`)
    .join("\n");

  assert.equal(scripts.package, "npm run package:vsix");
  assert.match(scripts.clean, /maxRetries:\s*5/);
  assert.match(scripts.clean, /retryDelay:\s*200/);

  // A VSIX built without the staged managed runtime installs and is then completely non-functional,
  // and nothing about the build output reveals that. Release packaging therefore runs through a
  // guard that refuses to produce an artifact unless the signed runtime is present for a declared
  // target; the unguarded path survives only under a name nobody would publish by accident.
  assert.match(scripts["package:vsix"], /scripts\/package-release\.js/);
  assert.match(scripts["package:pre-release"], /scripts\/package-release\.js --pre-release/);
  assert.doesNotMatch(scripts["package:vsix"], /\bvsce package\b/);
  assert.match(scripts["package:dev-vsix"], /vsce package --no-dependencies/);
  assert.equal(existsSync(resolve(packageRoot, "scripts/package-release.js")), true);

  assert.match(scripts["test:production-install"], /production\/runTest\.js/);
  assert.match(scripts["prepublish:check"], /npm audit --audit-level=high/);
  assert.match(scripts["prepublish:check"], /npm run package:vsix/);
  assert.match(scripts["publish:dry-run"], /npm run prepublish:check/);
  assert.match(scripts["publish:dry-run"], /vsce ls --tree --no-dependencies/);
  assert.doesNotMatch(scriptText, /--allow-missing-repository/);
  assert.doesNotMatch(scriptText, /\bvsce publish\b/);
});

test("release packaging refuses to build a VSIX without the managed runtime", () => {
  const packageRoot = resolve(__dirname, "../..");
  const guard = readFileSync(resolve(packageRoot, "scripts/package-release.js"), "utf8");

  assert.match(guard, /resources", "managed-cli"/);
  assert.match(guard, /manifest\.json/);
  assert.match(guard, /ALYSIS_VSIX_TARGET/);
  assert.match(guard, /artifact\.signature/);
  assert.match(guard, /createManagedCliReleaseSignatureVerifier/);
  assert.match(guard, /process\.exit\(1\)/);
});

test("package.json contributes optional native chat participant without Copilot dependency", () => {
  const packageRoot = resolve(__dirname, "../..");
  const packageJson = JSON.parse(readFileSync(resolve(packageRoot, "package.json"), "utf8")) as {
    activationEvents?: string[];
    contributes: { chatParticipants?: Array<{ id: string; name: string; commands?: Array<{ name: string }> }> };
    dependencies?: Record<string, string>;
    devDependencies?: Record<string, string>;
  };

  // Contributed participants activate implicitly; an explicit event would be redundant.
  assert.equal(packageJson.activationEvents?.includes("onChatParticipant:alysisai.alysis"), false);
  assert.equal(packageJson.contributes.chatParticipants?.length, 1);
  const participant = packageJson.contributes.chatParticipants?.[0];
  assert.equal(participant?.id, "alysisai.alysis");
  assert.equal(participant?.name, "alysis");
  assert.deepEqual(
    new Set((participant?.commands ?? []).map((command) => command.name)),
    new Set(["help", "forge", "execute", "doctor"])
  );
  assert.equal(Boolean(packageJson.dependencies?.["@github/copilot"] || packageJson.devDependencies?.["@github/copilot"]), false);
});

test("VS Code type definitions match the declared minimum engine", () => {
  const packageRoot = resolve(__dirname, "../..");
  const packageJson = JSON.parse(readFileSync(resolve(packageRoot, "package.json"), "utf8")) as {
    engines: { vscode: string };
    devDependencies: Record<string, string>;
  };
  const packageLock = JSON.parse(readFileSync(resolve(packageRoot, "package-lock.json"), "utf8")) as {
    packages: Record<string, { version?: string; devDependencies?: Record<string, string> }>;
  };
  const minimumEngine = minimumVersion(packageJson.engines.vscode);

  assert.equal(packageJson.devDependencies["@types/vscode"], minimumEngine);
  assert.equal(packageLock.packages[""]?.devDependencies?.["@types/vscode"], minimumEngine);
  assert.equal(packageLock.packages["node_modules/@types/vscode"]?.version, minimumEngine);
  const vscodeTypes = readFileSync(resolve(packageRoot, "node_modules/@types/vscode/index.d.ts"), "utf8");
  assert.match(vscodeTypes, /namespace chat/);
  assert.match(vscodeTypes, /createChatParticipant\(id: string, handler: ChatRequestHandler\): ChatParticipant/);
});

test("integration runner supports cached VS Code executable path", () => {
  const packageRoot = resolve(__dirname, "../..");
  const packageJson = JSON.parse(readFileSync(resolve(packageRoot, "package.json"), "utf8")) as {
    scripts: Record<string, string>;
  };
  const runner = readFileSync(resolve(packageRoot, "test/integration/runTest.ts"), "utf8");

  assert.match(packageJson.scripts["test:integration"], /out\/test\/integration\/runTest\.js/);
  assert.match(runner, /VSCODE_TEST_EXECUTABLE_PATH/);
  assert.match(runner, /vscodeExecutablePath/);
  assert.match(runner, /extensionHostDownloadFailure/);
  assert.match(runner, /preflightDownload/);
  assert.match(runner, /runVsCodeExtensionHostTests\(\{/);
  assert.match(runner, /finally\s*\{/);
  assert.match(runner, /removeTempDirectory\(tempRoot\)/);
  assert.match(runner, /copyTestDependencyTree/);
  // A task-specific executable hint such as ALYSIS_TEST_NODE_PATH does not change Node's module lookup.
  assert.doesNotMatch(runner, /\bNODE_PATH\b/);
});

test("passive activation and status refresh do not call update.check", () => {
  const packageRoot = resolve(__dirname, "../..");
  for (const relative of [
    "src/extension.ts",
    "src/status/statusBar.ts",
    "src/chat/CockpitRuntimeState.ts",
    "src/forge/ForgeController.ts"
  ]) {
    const source = readFileSync(resolve(packageRoot, relative), "utf8");
    assert.doesNotMatch(source, /\.updateCheck\(/, relative);
    assert.doesNotMatch(source, /"update\.check"/, relative);
  }
});

test("activation, tree refresh, and health refresh do not perform passive network management calls", () => {
  const packageRoot = resolve(__dirname, "../..");
  for (const relative of [
    "src/extension.ts",
    "src/status/statusBar.ts",
    "src/chat/CockpitRuntimeState.ts",
    "src/backend/ProfileSnapshotStore.ts",
    "src/commands/runDoctor.ts",
    "src/views/manageView.ts",
    "src/views/sessionsView.ts"
  ]) {
    const source = readFileSync(resolve(packageRoot, relative), "utf8");
    assert.doesNotMatch(source, /\.updateCheck\(/, relative);
    assert.doesNotMatch(source, /"update\.check"/, relative);
    assert.doesNotMatch(source, /\.extSearch\(/, relative);
    assert.doesNotMatch(source, /"ext\.search"/, relative);
    assert.doesNotMatch(source, /allow_network:\s*true/, relative);
  }
});

test("session tree context commands propagate selected session ids only for selected-session routes", async () => {
  const registered = new Map<string, (...args: unknown[]) => unknown>();
  const warnings: string[] = [];
  const vscodeStub = {
    commands: {
      registerCommand: (command: string, callback: (...args: unknown[]) => unknown) => {
        registered.set(command, callback);
        return { dispose: () => undefined };
      }
    },
    window: {
      showWarningMessage: async (message: string) => {
        warnings.push(message);
        return undefined;
      }
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

  try {
    const { registerSessionCommands } =
      require("../src/commands/sessionCommands") as typeof import("../src/commands/sessionCommands");
    const calls: Array<{ actionId: string; args: string }> = [];
    registerSessionCommands(
      { subscriptions: [] } as any,
      {
        executeAction: async (actionId: string, args: string) => {
          calls.push({ actionId, args });
        }
      } as any
    );

    const selectedItem = { sessionId: "selected-session", label: "wrong-label" };
    for (const command of [
      "alysis.session.show",
      "alysis.session.score",
      "alysis.session.usage",
      "alysis.session.resume",
      "alysis.session.compact",
      "alysis.session.clear"
    ]) {
      await Promise.resolve(registered.get(command)?.(selectedItem));
    }

    assert.deepEqual(calls, [
      { actionId: "session.show", args: "selected-session" },
      { actionId: "session.score", args: "selected-session" },
      { actionId: "session.usage", args: "selected-session" },
      { actionId: "session.resume", args: "selected-session" },
      { actionId: "session.compact", args: "" },
      { actionId: "session.clear", args: "" }
    ]);
    assert.deepEqual(warnings, []);
  } finally {
    moduleLoader._load = originalLoad;
  }
});

test("package.json contributes no menus bound to removed tree views", () => {
  const packageRoot = resolve(__dirname, "../..");
  const packageJson = JSON.parse(readFileSync(resolve(packageRoot, "package.json"), "utf8")) as {
    contributes: { menus?: Record<string, Array<{ command: string; when?: string; group?: string }>> };
  };
  const menus = packageJson.contributes.menus ?? {};
  const removedViews = ["alysis.recentWork", "alysis.plan", "alysis.results", "alysis.connections"];

  // The tree views these menus hung off were `when: "false"` and could never render, which made
  // every entry unreachable. Removing the views without removing the menus would leave contributions
  // pointing at view ids that no longer exist.
  assert.equal(menus["view/item/context"], undefined, "no tree items remain to attach a context menu to");
  for (const [section, entries] of Object.entries(menus)) {
    for (const entry of entries) {
      for (const view of removedViews) {
        assert.equal(
          (entry.when ?? "").includes(view),
          false,
          `${section} entry for ${entry.command} still references ${view}`
        );
      }
    }
  }

  // Every remaining view/title entry belongs to the one surviving view.
  for (const entry of menus["view/title"] ?? []) {
    assert.match(entry.when ?? "", /view == alysis\.start/, entry.command);
  }
});

test("manage tree context commands pass the selected item target to backend actions", async () => {
  const registered = new Map<string, (...args: unknown[]) => unknown>();
  const vscodeStub = {
    commands: {
      registerCommand: (command: string, callback: (...args: unknown[]) => unknown) => {
        registered.set(command, callback);
        return { dispose: () => undefined };
      }
    },
    window: { showWarningMessage: async () => undefined },
    TreeItem: class {
      public description?: string;
      public contextValue?: string;
      public tooltip?: string;
      public constructor(public label: string, public collapsibleState: unknown) {}
    },
    TreeItemCollapsibleState: { None: 0, Collapsed: 1 },
    EventEmitter: class {
      public event = (): { dispose(): void } => ({ dispose: () => undefined });
      public fire(): void {}
    },
    ThemeIcon: class {
      public constructor(public id: string) {}
    },
    workspace: { workspaceFolders: [{ uri: { fsPath: "/workspace/project" } }], isTrusted: true }
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

  try {
    const { ManageTreeItem } = require("../src/views/manageView") as typeof import("../src/views/manageView");
    const { registerManageCommands } =
      require("../src/commands/manageCommands") as typeof import("../src/commands/manageCommands");
    const calls: Array<{ actionId: string; args: string }> = [];
    let refreshes = 0;
    registerManageCommands(
      { subscriptions: [] } as any,
      {
        executeAction: async (actionId: string, args: string) => {
          calls.push({ actionId, args });
        }
      } as any,
      { refresh: () => { refreshes += 1; } } as any
    );

    const convention = new ManageTreeItem(
      "item",
      "conventions",
      "docs/conventions.md",
      "docs/conventions.md",
      vscodeStub.TreeItemCollapsibleState.None,
      "docs/conventions.md"
    );
    const mcp = new ManageTreeItem(
      "item",
      "mcp",
      "ctx7",
      "ctx7",
      vscodeStub.TreeItemCollapsibleState.None,
      "ctx7"
    );

    await Promise.resolve(registered.get("alysis.manage.info")?.(convention));
    await Promise.resolve(registered.get("alysis.manage.mcpLogout")?.(mcp));

    assert.deepEqual(calls, [
      { actionId: "conventions.render", args: "docs/conventions.md" },
      { actionId: "mcp.auth.logout", args: "ctx7" }
    ]);
    assert.equal(refreshes, 2);
  } finally {
    moduleLoader._load = originalLoad;
  }
});

test("manage refresh command awaits failures and reports them instead of rejecting silently", async () => {
  const registered = new Map<string, (...args: unknown[]) => unknown>();
  const warnings: string[] = [];
  const vscodeStub = {
    commands: {
      registerCommand: (command: string, callback: (...args: unknown[]) => unknown) => {
        registered.set(command, callback);
        return { dispose: () => undefined };
      }
    },
    window: {
      showWarningMessage: async (message: string) => {
        warnings.push(message);
      }
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

  try {
    const manageCommandsPath = require.resolve("../src/commands/manageCommands");
    delete require.cache[manageCommandsPath];
    const { registerManageCommands } =
      require("../src/commands/manageCommands") as typeof import("../src/commands/manageCommands");
    let resyncs = 0;
    registerManageCommands(
      { subscriptions: [] } as any,
      { executeAction: async () => undefined } as any,
      {
        refresh: async () => {
          throw new Error("bridge failed with Bearer abcdefgh1234567890");
        }
      } as any,
      () => {
        resyncs += 1;
      }
    );

    const result = registered.get("alysis.manage.refresh")?.();
    assert.ok(result instanceof Promise, "VS Code can await the refresh command lifecycle");
    await result;

    assert.equal(resyncs, 1);
    assert.equal(warnings.length, 1);
    assert.match(warnings[0], /Could not refresh Alysis Code settings/);
    assert.doesNotMatch(warnings[0], /abcdefgh1234567890/);
  } finally {
    moduleLoader._load = originalLoad;
  }
});

test("internal manage and session commands stay out of the Command Palette", () => {
  const packageRoot = resolve(__dirname, "../..");
  const packageJson = JSON.parse(readFileSync(resolve(packageRoot, "package.json"), "utf8")) as {
    contributes: {
      commands: Array<{ command: string }>;
      menus?: { commandPalette?: Array<{ command: string; when?: string }> };
    };
  };
  const suppressed = new Map(
    (packageJson.contributes.menus?.commandPalette ?? []).map((entry) => [entry.command, entry.when ?? ""])
  );

  // These are routed programmatically from the sidebar, not typed by a user. With the tree views
  // gone they have no menu of their own, so palette suppression is the only thing keeping the
  // palette free of entries like a bare "Alysis Code: Refresh" that does nothing observable.
  const internal = packageJson.contributes.commands
    .map((entry) => entry.command)
    .filter((command) => command.startsWith("alysis.manage.") || command.startsWith("alysis.session."));

  assert.ok(internal.length > 0, "internal command families still exist");
  for (const command of internal) {
    assert.equal(suppressed.get(command), "false", `${command} must be suppressed from the palette`);
  }
});

test("/permissions uses live session backend first and default config fallback otherwise", () => {
  const packageRoot = resolve(__dirname, "../..");
  const extensionSource = readFileSync(resolve(packageRoot, "src/extension.ts"), "utf8");
  const routingSource = readFileSync(resolve(packageRoot, "src/slash/modeRouting.ts"), "utf8");

  assert.match(extensionSource, /routeSlashModeChange\(mode/);
  assert.match(extensionSource, /backendActions\.executeSlashAction\(actionId, args\)/);
  assert.match(extensionSource, /getConfiguration\("alysis"\)\.update\(\s*"defaultMode",\s*nextMode,\s*vscode\.ConfigurationTarget\.Global/s);
  assert.match(routingSource, /executeBackendAction\("session\.setMode", mode\)/);
  assert.match(routingSource, /updateDefaultMode\(mode\)/);
});

test("component candidate workflow cannot be mistaken for production or Marketplace publish", () => {
  const packageRoot = resolve(__dirname, "../..");
  const repoRoot = resolve(packageRoot, "../..");
  const workflow = readFileSync(
    resolve(repoRoot, ".github/workflows/vscode-extension-release-candidate.yml"),
    "utf8"
  );
  const localScriptPath = resolve(repoRoot, "scripts/qa/check_vscode_extension_release_candidate.sh");
  const localScript = readFileSync(localScriptPath, "utf8");

  assert.match(workflow, /workflow_dispatch/);
  assert.doesNotMatch(workflow, /manual_real_provider_report/);
  assert.doesNotMatch(workflow, /no_p0_p1_blockers/);
  assert.doesNotMatch(workflow, /known_limitations_confirmed/);
  assert.match(workflow, /Validate cached VS Code input/);
  assert.match(workflow, /vscode_current_test_version/);
  assert.match(workflow, /vscode_current_executable_path/);
  assert.match(workflow, /dogfood_extension_host == 'require-cached'.*inputs\.vscode_executable_path == ''.*inputs\.vscode_current_executable_path == ''/);
  assert.match(workflow, /dogfood_extension_host=require-cached requires both vscode_executable_path and vscode_current_executable_path/);
  assert.equal(existsSync(resolve(repoRoot, "tests/test_surface_console_encoding.py")), true);
  assert.match(workflow, /python scripts\/qa\/check_ide_cli_parity\.py/);
  assert.match(
    workflow,
    /python -m pytest -q tests\/test_ide_cli_parity_matrix\.py tests\/test_ide_protocol\.py tests\/test_ide_stdio_bridge\.py tests\/test_ide_protocol_contract\.py/
  );
  assert.match(workflow, /tests\/test_surface_console_encoding\.py/);
  assert.match(workflow, /tests\/test_forge_exec\.py/);
  assert.match(workflow, /tests\/test_forge_swarm\.py/);
  assert.match(workflow, /tests\/test_forge_review\.py/);
  assert.match(workflow, /npm audit --audit-level=high/);
  assert.match(workflow, /Package component pre-release VSIX/);
  assert.match(workflow, /npm run package:dev-vsix -- --pre-release/);
  assert.match(workflow, /Verify pre-release VSIX marker and contents/);
  assert.match(workflow, /Microsoft\.VisualStudio\.Code\.PreRelease/);
  assert.match(workflow, /extension\/dist\/extension\.js/);
  assert.match(workflow, /forbidden_prefixes/);
  assert.doesNotMatch(workflow, /^\s*run: npm run package\s*$/m);
  assert.match(workflow, /Test extension host \(minimum supported VS Code\)/);
  assert.match(workflow, /Test extension host \(current VS Code\)/);
  assert.equal((workflow.match(/npm run test:integration/g) ?? []).length >= 2, true);
  assert.match(workflow, /vscode_extension_dogfood\.py/);
  assert.match(workflow, /validate_component_valid_summary/);
  assert.doesNotMatch(workflow, /validate_release_valid_summary\(payload\)/);
  assert.match(workflow, /must not package a production-trusted managed runtime/);
  assert.match(workflow, /never produces a releasable VSIX/);
  assert.match(workflow, /not-for-release/);
  assert.match(workflow, /actions\/upload-artifact@[0-9a-f]{40}\s+#\s+v4/);
  assert.doesNotMatch(workflow, /\bvsce publish\b/);

  assert.equal(existsSync(localScriptPath), true);
  assert.match(localScript, /set -Eeuo pipefail/);
  assert.match(localScript, /scripts\/qa\/check_ide_cli_parity\.py/);
  assert.match(localScript, /tests\/test_ide_cli_parity_matrix\.py/);
  assert.match(localScript, /tests\/test_ide_protocol\.py/);
  assert.match(localScript, /tests\/test_ide_stdio_bridge\.py/);
  assert.match(localScript, /tests\/test_ide_protocol_contract\.py/);
  assert.match(localScript, /\bnpm ci\b/);
  assert.match(localScript, /\bnpm test\b/);
  assert.match(localScript, /\bnpm run lint\b/);
  assert.match(localScript, /\bnpm audit --audit-level=high\b/);
  assert.match(localScript, /package:pre-release/);
  assert.match(localScript, /Verify pre-release VSIX marker and contents/);
  assert.match(localScript, /extension\/dist\/extension\.js/);
  assert.match(localScript, /forbidden_prefixes/);
  assert.match(localScript, /no LLM\/provider credentials are required/);
});

function minimumVersion(range: string): string {
  const match = range.match(/^\^(\d+\.\d+\.\d+)$/);
  assert.ok(match, `expected VS Code engine range like ^1.90.0, got ${range}`);
  return match[1];
}
