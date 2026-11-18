import assert from "node:assert/strict";
import Module from "node:module";
import test from "node:test";

const createdParticipants: Array<{ id: string; handler: Function; iconPath?: unknown; dispose(): void }> = [];
const vscodeStub = {
  chat: {
    createChatParticipant: (id: string, handler: Function) => {
      const participant = {
        id,
        handler,
        iconPath: undefined as unknown,
        dispose: () => undefined
      };
      createdParticipants.push(participant);
      return participant;
    }
  },
  Uri: {
    joinPath: (...parts: unknown[]) => ({ parts })
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

const {
  registerAlysisChatParticipant,
  AlysisChatParticipantRouter,
  ALYSIS_CHAT_PARTICIPANT_ID
} = require("../src/chatParticipant/AlysisChatParticipant") as typeof import("../src/chatParticipant/AlysisChatParticipant");
moduleLoader._load = originalLoad;

test("registerAlysisChatParticipant creates the stable VS Code 1.90 participant", () => {
  createdParticipants.length = 0;
  const subscriptions: unknown[] = [];

  registerAlysisChatParticipant(
    { extensionUri: { fsPath: "/extension" }, subscriptions } as any,
    {
      openCockpit: async () => undefined,
      slashRouter: { execute: async () => slashResult("ok") }
    }
  );

  assert.equal(createdParticipants.length, 1);
  assert.equal(createdParticipants[0].id, ALYSIS_CHAT_PARTICIPANT_ID);
  assert.equal(subscriptions.includes(createdParticipants[0]), true);
  assert.deepEqual(createdParticipants[0].iconPath, { parts: [{ fsPath: "/extension" }, "resources", "alysis-logo.png"] });
});

test("@alysis /help opens the primary sidebar conversation", async () => {
  const opened: string[] = [];
  const router = new AlysisChatParticipantRouter({
    openCockpit: async () => {
      opened.push("open");
    },
    slashRouter: { execute: async () => slashResult("must not run") }
  });
  const response = createResponseStream();

  const result = await router.handle({ command: "help", prompt: "" }, response.stream);

  assert.deepEqual(opened, ["open"]);
  assert.equal(result.metadata?.command, "help");
  assert.match(response.markdown.join("\n"), /Alysis Code sidebar is the primary conversation/);
  assert.match(response.markdown.join("\n"), /Native @alysis commands/);
  assert.deepEqual(response.buttons.map((button) => button.command), ["alysis.openChat"]);
});

test("@alysis retired plan commands are unknown and never reach the slash router", async () => {
  let executed = 0;
  const router = new AlysisChatParticipantRouter({
    openCockpit: async () => undefined,
    slashRouter: {
      execute: async () => {
        executed += 1;
        return slashResult("must not run");
      }
    }
  });

  for (const request of [
    { command: "forge", prompt: "implement fixture cleanup" },
    { command: "execute", prompt: "" },
    { prompt: "/forge plan implement fixture cleanup" },
    { prompt: "/execute preview" }
  ]) {
    const result = await router.handle(request, createResponseStream().stream);
    assert.equal(result.metadata?.command, "unknown", JSON.stringify(request));
  }
  assert.equal(executed, 0);
});

test("@alysis /doctor routes through Cockpit doctor", async () => {
  const executed: string[] = [];
  const router = new AlysisChatParticipantRouter({
    openCockpit: async () => undefined,
    slashRouter: {
      execute: async (input: string) => {
        executed.push(input);
        return slashResult("Routed /doctor to Alysis Code doctor.");
      }
    }
  });

  const result = await router.handle({ command: "doctor", prompt: "" }, createResponseStream().stream);

  assert.deepEqual(executed, ["/doctor"]);
  assert.equal(result.metadata?.command, "doctor");
});

test("@alysis normal chat opens Cockpit instead of silently sending text to the model", async () => {
  let opened = 0;
  let executed = 0;
  const router = new AlysisChatParticipantRouter({
    openCockpit: async () => {
      opened += 1;
    },
    slashRouter: {
      execute: async () => {
        executed += 1;
        return slashResult("must not run");
      }
    }
  });
  const response = createResponseStream();

  const result = await router.handle({ prompt: "write code for me" }, response.stream);

  assert.equal(opened, 1);
  assert.equal(executed, 0);
  assert.equal(result.metadata?.command, "openCockpit");
  assert.match(response.markdown.join("\n"), /Use the sidebar to chat/);
});

test("@alysis unknown slash command is not routed to chat or model", async () => {
  let executed = 0;
  const router = new AlysisChatParticipantRouter({
    openCockpit: async () => undefined,
    slashRouter: {
      execute: async () => {
        executed += 1;
        return slashResult("must not run");
      }
    }
  });
  const response = createResponseStream();

  const result = await router.handle({ prompt: "/unknown sk-testsecretvalue" }, response.stream);

  assert.equal(executed, 0);
  assert.equal(result.metadata?.command, "unknown");
  assert.match(response.markdown.join("\n"), /Unknown @alysis slash command/);
  assert.doesNotMatch(response.markdown.join("\n"), /sk-testsecretvalue/);
});

test("@alysis surfaces Workspace Trust blocks from slash router", async () => {
  const router = new AlysisChatParticipantRouter({
    openCockpit: async () => undefined,
    slashRouter: {
      execute: async () => slashResult("/doctor requires Workspace Trust.", "warning")
    }
  });
  const response = createResponseStream();

  const result = await router.handle({ command: "doctor", prompt: "" }, response.stream);

  assert.equal(result.errorDetails, undefined);
  assert.match(response.markdown.join("\n"), /Workspace Trust/);
  assert.match(response.markdown.join("\n"), /Alysis Code command warning/);
});

test("@alysis adds recovery guidance for missing CLI without forwarding secrets", async () => {
  const router = new AlysisChatParticipantRouter({
    openCockpit: async () => undefined,
    slashRouter: {
      execute: async () => slashResult("Routed /doctor to Alysis Code doctor.")
    },
    runtime: {
      snapshot: () =>
        ({
          cliHealth: { status: "missing", message: "Alysis Code CLI was not found." },
          bridgeProtocol: { status: "unknown", message: null }
        }) as any
    }
  });
  const response = createResponseStream();

  await router.handle({ command: "doctor", prompt: "" }, response.stream);

  const markdown = response.markdown.join("\n");
  assert.match(markdown, /CLI Health is missing/);
  assert.match(markdown, /does not forward API keys/);
});

test("@alysis redacts secrets from thrown command errors", async () => {
  const router = new AlysisChatParticipantRouter({
    openCockpit: async () => undefined,
    slashRouter: {
      execute: async () => {
        throw new Error("backend failed with sk-testsecretvalue");
      }
    }
  });
  const response = createResponseStream();

  const result = await router.handle({ command: "doctor", prompt: "" }, response.stream);

  const markdown = response.markdown.join("\n");
  assert.equal(result.metadata?.command, "doctor");
  assert.match(markdown, /backend failed with \\?<redacted\\?>/);
  assert.doesNotMatch(markdown, /sk-testsecretvalue/);
  assert.doesNotMatch(result.errorDetails?.message ?? "", /sk-testsecretvalue/);
});

function createResponseStream(): {
  stream: { markdown(value: string): void; progress(value: string): void; button(command: any): void };
  markdown: string[];
  progress: string[];
  buttons: any[];
} {
  const markdown: string[] = [];
  const progress: string[] = [];
  const buttons: any[] = [];
  return {
    markdown,
    progress,
    buttons,
    stream: {
      markdown: (value: string) => markdown.push(value),
      progress: (value: string) => progress.push(value),
      button: (command: any) => buttons.push(command)
    }
  };
}

function slashResult(notice: string, severity: "info" | "warning" | "error" = "info") {
  return {
    handled: true,
    command: "/test",
    notice,
    severity
  };
}
