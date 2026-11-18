import assert from "node:assert/strict";
import test from "node:test";

import { ChatTranscript } from "../src/chat/ChatTranscript";
import { PROTOCOL_VERSION, ProtocolEventEnvelope } from "../src/client/AlysisProtocol";

test("unverified task completion remains terminal and does not become success", () => {
  const transcript = new ChatTranscript();
  transcript.applyEvent(event({ sequence: 1, type: "activity_update", payload: {
    activity_id: "prompt-verdict", kind: "plan", operation: "execute_prompt",
    status: "completed_unverified", display_title: "Request finished · unverified"
  } }));
  const item = transcript.state().items.find((value) => value.kind === "tool");
  assert.equal(item?.status, "completed_unverified");
});

test("repeated runner failures retain tool rows and show one setup action per job", () => {
  const transcript = new ChatTranscript();
  const failure = "failed to connect to the docker API at npipe:////./pipe/docker_engine";
  for (let sequence = 1; sequence <= 3; sequence++) {
    transcript.applyEvent(event({ sequence, job_id: "job-1", type: "tool_call_completed", payload: {
      call_id: `tool-${sequence}`, success: false, result_preview: failure
    } }));
  }
  transcript.applyEvent(event({ sequence: 4, job_id: "job-1", type: "activity_update", payload: {
    activity_id: "tool-4", kind: "test", status: "failed", display_title: "Run tests", summary: failure
  } }));
  let state = transcript.state();
  assert.equal(state.items.filter((item) => item.kind === "tool").length, 4);
  assert.equal(state.items.filter((item) => item.error?.kind === "sandbox_unavailable").length, 1);
  assert.equal(state.items.find((item) => item.kind === "error")?.actions?.[0].command, "alysis.runDoctor");
  transcript.applyEvent(event({ sequence: 5, job_id: "job-2", type: "tool_call_completed", payload: {
    call_id: "tool-5", success: false, result_preview: failure
  } }));
  state = transcript.state();
  assert.equal(state.items.filter((item) => item.error?.kind === "sandbox_unavailable").length, 2);
});

test("checkpoint failures show one recovery card per job", () => {
  const transcript = new ChatTranscript();
  for (let sequence = 1; sequence <= 3; sequence++) {
    transcript.applyEvent(event({ sequence, job_id: "job-1", type: "tool_call_completed", payload: {
      call_id: `edit-${sequence}`, success: false, result_preview: "checkpoint_unavailable: setup pending"
    } }));
  }
  assert.equal(transcript.state().items.filter((item) => item.error?.kind === "checkpoint_unavailable").length, 1);
});

test("ordinary test failures and successful output do not show command setup recovery", () => {
  const transcript = new ChatTranscript();
  transcript.applyEvent(event({ sequence: 1, type: "tool_call_completed", payload: {
    call_id: "test-1", success: false, result_preview: "AssertionError: expected 2, got 3"
  } }));
  transcript.applyEvent(event({ sequence: 2, type: "tool_call_completed", payload: {
    call_id: "read-1", success: true, result_preview: "failed to connect to the docker API"
  } }));
  assert.equal(transcript.state().items.filter((item) => item.kind === "error").length, 0);
});

test("ChatTranscript accumulates streaming assistant messages", () => {
  const transcript = new ChatTranscript();
  transcript.setSession({ session_id: "session-1", workspace_root: "/workspace", mode: "readonly" });
  transcript.applyEvent(event({ sequence: 1, type: "message_delta", payload: { text: "Hel" } }));
  transcript.applyEvent(event({ sequence: 2, type: "message_delta", payload: { text: "lo" } }));
  transcript.applyEvent(event({ sequence: 3, type: "message_end", payload: {} }));

  const state = transcript.state();
  assert.equal(state.items.length, 1);
  assert.equal(state.items[0].kind, "assistant");
  assert.equal(state.items[0].text, "Hello");
  assert.equal(state.lastSequence, 3);
});

test("ChatTranscript restores speaker roles and repeated answers across user turns", () => {
  const transcript = new ChatTranscript();
  for (const [index, [role, text]] of [
    ["user", "First request"],
    ["assistant", "OK"],
    ["user", "Second request"],
    ["assistant", "OK"]
  ].entries()) {
    transcript.applyEvent(event({ sequence: index + 1, type: "message_end", payload: { role, text } }));
  }
  assert.deepEqual(transcript.state().items.map(({ kind, title, text }) => ({ kind, title, text })), [
    { kind: "user", title: "You", text: "First request" },
    { kind: "assistant", title: "Alysis Code", text: "OK" },
    { kind: "user", title: "You", text: "Second request" },
    { kind: "assistant", title: "Alysis Code", text: "OK" }
  ]);
});

test("ChatTranscript keeps a replayed user message separate from partial assistant text", () => {
  const transcript = new ChatTranscript();
  transcript.applyEvent(event({ sequence: 1, type: "message_delta", payload: { text: "Partial answer" } }));
  transcript.applyEvent(event({ sequence: 2, type: "message_end", payload: { role: "user", text: "Next request" } }));
  transcript.applyEvent(event({ sequence: 3, type: "message_end", payload: { text: "Next answer" } }));
  assert.deepEqual(transcript.state().items.map(item => [item.kind, item.text]), [
    ["assistant", "Partial answer"], ["user", "Next request"], ["assistant", "Next answer"]
  ]);
});

test("ChatTranscript renders tool calls as structured cards", () => {
  const transcript = new ChatTranscript();
  transcript.applyEvent(event({ sequence: 1, type: "tool_call_started", payload: { call_id: "c1", name: "fs_read", arguments_preview: "README.md" } }));
  transcript.applyEvent(event({ sequence: 2, type: "tool_call_progress", payload: { call_id: "c1", text: "reading" } }));
  transcript.applyEvent(event({ sequence: 3, type: "tool_call_completed", payload: { call_id: "c1", success: true, result_preview: "ok" } }));

  const tool = transcript.state().items[0];
  assert.equal(tool.kind, "tool");
  assert.equal(tool.title, "fs_read");
  assert.equal(tool.status, "ok");
  assert.equal(tool.text, "ok");
  assert.deepEqual(tool.metadata, {
    call_id: "c1",
    tool_name: "fs_read",
    input_preview: "README.md"
  });
});

test("ChatTranscript uses advertised semantic activity as the canonical tool timeline", () => {
  const transcript = new ChatTranscript();
  transcript.setSemanticActivityEnabled(true);

  transcript.applyEvent(event({
    sequence: 1,
    type: "tool_call_started",
    payload: { call_id: "c1", name: "rs.read", arguments_preview: "{\"path\":\"src/app.ts\"}" }
  }));
  transcript.applyEvent(event({
    sequence: 2,
    type: "activity_update",
    payload: {
      activity_id: "c1",
      kind: "read",
      operation: "read_file",
      display_title: "Read file",
      status: "running",
      target: "src/app.ts",
      patch: "must never appear"
    }
  }));
  transcript.applyEvent(event({
    sequence: 3,
    type: "tool_call_completed",
    payload: { call_id: "c1", success: true, result_preview: "raw tool output" }
  }));
  transcript.applyEvent(event({
    sequence: 4,
    type: "activity_update",
    payload: {
      activity_id: "c1",
      kind: "read",
      operation: "read_file",
      display_title: "Read file",
      status: "succeeded",
      target: "src/app.ts",
      summary: "Inspected the requested source file.",
      duration_ms: 125
    }
  }));

  const state = transcript.state();
  assert.equal(state.items.length, 1);
  assert.deepEqual(
    {
      kind: state.items[0].kind,
      title: state.items[0].title,
      status: state.items[0].status,
      text: state.items[0].text
    },
    {
      kind: "tool",
      title: "Read file",
      status: "ok",
      text: "Inspected the requested source file. · 125 ms"
    }
  );
  assert.equal(JSON.stringify(state.items).includes("rs.read"), false);
  assert.equal(JSON.stringify(state.items).includes("read_file"), false);
  assert.equal(JSON.stringify(state.items).includes("activity_id"), false);
  assert.equal(JSON.stringify(state.items).includes("display_title"), false);
  assert.equal(JSON.stringify(state.items).includes("raw tool output"), false);
  assert.equal(JSON.stringify(state.items).includes("must never appear"), false);
});

test("ChatTranscript keeps a legacy tool row when semantic activity is advertised but never arrives for that call", () => {
  // CLI 0.14 advertises activity_update and emits it for patches and prompts, but verify_run /
  // shell_run reach the IDE as legacy tool events only. Dropping every legacy event on the strength
  // of the advertisement made those executions vanish between their approval card and the reply.
  const transcript = new ChatTranscript();
  transcript.setSemanticActivityEnabled(true);

  transcript.applyEvent(event({
    sequence: 1,
    type: "tool_call_started",
    payload: { call_id: "verify-1", name: "verify_run", arguments_preview: "{\"commands\":[\"pytest -q\"]}" }
  }));
  transcript.applyEvent(event({
    sequence: 2,
    type: "tool_call_progress",
    payload: { call_id: "verify-1", text: "collecting tests" }
  }));
  transcript.applyEvent(event({
    sequence: 3,
    type: "tool_call_completed",
    payload: { call_id: "verify-1", success: false, result_preview: "{\"exit_code\":1,\"error\":\"docker unavailable\"}" }
  }));

  const state = transcript.state();
  assert.equal(state.items.length, 1);
  assert.equal(state.items[0].kind, "tool");
  assert.equal(state.items[0].title, "verify_run");
  assert.equal(state.items[0].status, "failed");
  assert.equal(state.items[0].metadata?.tool_name, "verify_run");
  assert.match(String(state.items[0].text), /docker unavailable/);
});

test("ChatTranscript ignores late legacy events for a call a semantic activity already owns", () => {
  const transcript = new ChatTranscript();
  transcript.setSemanticActivityEnabled(true);

  transcript.applyEvent(event({
    sequence: 1,
    type: "tool_call_started",
    payload: { call_id: "c1", name: "shell_run", arguments_preview: "{\"cmd\":\"pytest -q\"}" }
  }));
  transcript.applyEvent(event({
    sequence: 2,
    type: "activity_update",
    payload: { activity_id: "c1", kind: "test", display_title: "Run tests", status: "running", target: "pytest -q" }
  }));
  transcript.applyEvent(event({
    sequence: 3,
    type: "tool_call_progress",
    payload: { call_id: "c1", text: "raw chunk" }
  }));
  transcript.applyEvent(event({
    sequence: 4,
    type: "tool_call_completed",
    payload: { call_id: "c1", success: true, result_preview: "raw result" }
  }));
  transcript.applyEvent(event({
    sequence: 5,
    type: "activity_update",
    payload: { activity_id: "c1", kind: "test", display_title: "Run tests", status: "succeeded", target: "pytest -q", summary: "3 passed" }
  }));

  const state = transcript.state();
  assert.equal(state.items.length, 1, "the activity owns the row; legacy events must not add or reopen one");
  assert.equal(state.items[0].title, "Run tests");
  assert.equal(state.items[0].status, "ok");
  assert.equal(JSON.stringify(state.items).includes("raw chunk"), false);
  assert.equal(JSON.stringify(state.items).includes("raw result"), false);
  assert.equal(JSON.stringify(state.items).includes("shell_run"), false);
});

test("ChatTranscript coalesces an observed semantic event over an earlier legacy row", () => {
  const transcript = new ChatTranscript();
  transcript.applyEvent(event({
    sequence: 1,
    type: "tool_call_started",
    payload: { call_id: "c1", name: "rs.read", arguments_preview: "private payload" }
  }));
  transcript.applyEvent(event({
    sequence: 2,
    type: "activity_update",
    payload: {
      activity_id: "c1",
      kind: "read",
      display_title: "Reading project files",
      status: "running",
      target: "src/index.ts"
    }
  }));

  const state = transcript.state();
  assert.equal(state.items.length, 1);
  assert.equal(state.items[0].title, "Reading project files");
  assert.equal(state.items[0].text, "src/index.ts");
  assert.equal(JSON.stringify(state.items).includes("rs.read"), false);
  assert.equal(JSON.stringify(state.items).includes("private payload"), false);
});

test("ChatTranscript bounds semantic activity and never projects patch bodies or internal identifiers", () => {
  const transcript = new ChatTranscript();
  const patch = `diff --git a/private b/private\n+API_KEY=${"secret".repeat(10_000)}`;
  transcript.applyEvent(event({
    sequence: 1,
    type: "activity_update",
    payload: {
      activity_id: "patch-1",
      kind: "edit",
      operation: "internal.apply_patch",
      display_title: "rs.read",
      status: "succeeded",
      target: "src/very-long.ts",
      summary: "Changed files safely. ".repeat(1_000),
      diff: { files: 2, additions: 10, deletions: 3 },
      patch,
      metadata: { internal_method: "rs.read" }
    }
  }));

  const item = transcript.state().items[0];
  assert.equal(item.title, "Making changes");
  assert.ok(item.text.length <= 1_000);
  assert.match(item.text, /2 files · \+10 · −3/);
  const visible = JSON.stringify(item);
  assert.equal(visible.includes("API_KEY"), false);
  assert.equal(visible.includes("internal.apply_patch"), false);
  assert.equal(visible.includes("internal_method"), false);
  assert.equal(visible.includes("rs.read"), false);
});

test("ChatTranscript rejects unknown protocol events without adding conversation cards", () => {
  const transcript = new ChatTranscript();
  transcript.setSession({ session_id: "session-1", workspace_root: "/workspace", mode: "readonly" });
  const handled = transcript.applyEvent(event({
    sequence: 1,
    type: "internal.runtime_debug",
    payload: { raw_method: "rs.read", patch: "private patch body" }
  }));

  assert.equal(handled, false);
  assert.deepEqual(transcript.state().items, []);
  assert.equal(transcript.lastSequence(), 1);
});

test("ChatTranscript correlates concurrent helper lifecycles and keeps child tools attributable", () => {
  const transcript = new ChatTranscript();
  transcript.applyEvent(event({
    sequence: 1,
    type: "subagent_state_changed",
    payload: {
      subagent_run_id: "run-a",
      name: "explorer",
      mode: "readonly",
      state: "running",
      description: "Inspect queue recovery."
    }
  }));
  transcript.applyEvent(event({
    sequence: 2,
    type: "subagent_state_changed",
    payload: {
      subagent_run_id: "run-b",
      name: "explorer",
      mode: "readonly",
      state: "running",
      description: "Inspect permission fencing."
    }
  }));
  transcript.applyEvent(event({
    sequence: 3,
    type: "tool_call_started",
    payload: {
      call_id: "subagent:explorer:read-1",
      name: "fs_read",
      arguments_preview: '{"path":"README.md"}',
      worker_id: "explorer",
      role: "readonly"
    }
  }));
  transcript.applyEvent(event({
    sequence: 4,
    type: "subagent_state_changed",
    payload: {
      subagent_run_id: "run-b",
      name: "explorer",
      mode: "readonly",
      state: "failed",
      subagent_session_id: "child-b",
      elapsed_ms: 500,
      steps_completed: 1,
      error: "The inspection could not finish."
    }
  }));
  transcript.applyEvent(event({
    sequence: 5,
    type: "subagent_state_changed",
    payload: {
      subagent_run_id: "run-a",
      name: "explorer",
      mode: "readonly",
      state: "success",
      subagent_session_id: "child-a",
      elapsed_ms: 1250,
      steps_completed: 3
    }
  }));

  const state = transcript.state();
  const helpers = state.items.filter((item) => item.metadata?.tool_name === "subagent");
  assert.equal(helpers.length, 2);
  assert.equal(helpers.find((item) => item.metadata?.subagent_run_id === "run-a")?.status, "complete");
  assert.match(helpers.find((item) => item.metadata?.subagent_run_id === "run-a")?.text ?? "", /3 steps · 1\.3 s/);
  assert.equal(helpers.find((item) => item.metadata?.subagent_run_id === "run-b")?.status, "failed");
  assert.match(helpers.find((item) => item.metadata?.subagent_run_id === "run-b")?.text ?? "", /could not finish/);
  const childTool = state.items.find((item) => item.metadata?.call_id === "subagent:explorer:read-1");
  assert.equal(childTool?.metadata?.worker_id, "explorer");
  assert.equal(childTool?.metadata?.worker_role, "readonly");
});

test("ChatTranscript tracks approval prompts and decisions", () => {
  const transcript = new ChatTranscript();
  transcript.applyEvent(event({
    sequence: 1,
    type: "prompt_for_input",
    payload: {
      kind: "approval",
      approval_id: "approval-1",
      reason: "write file",
      preview: "README.md",
      files: ["README.md"],
      command: null
    }
  }));

  assert.equal(transcript.pendingApprovals().length, 1);
  transcript.resolveApproval("approval-1", "deny");

  const state = transcript.state();
  assert.equal(state.approvals.length, 0);
  assert.equal(state.items[0].status, "deny");
  assert.equal(state.items[0].text.includes("denied"), true);
});

test("ChatTranscript renders approval session scope support", () => {
  const transcript = new ChatTranscript();
  transcript.applyEvent(event({
    sequence: 1,
    type: "prompt_for_input",
    payload: {
      kind: "approval",
      approval_id: "approval-1",
      reason: "write file",
      preview: "README.md",
      files: ["README.md"],
      allow_for_session_supported: true,
      allow_for_session_scope: {
        type: "exact_file_set",
        files_hash: "a".repeat(64),
        file_count: 1,
        files: ["README.md"]
      },
      allow_for_session_warning: null
    }
  }));

  const item = transcript.state().items[0];
  assert.equal(item.text.includes("Scope: exact file set (1 file)"), true);
  assert.equal(item.metadata?.allow_for_session_supported, true);
  assert.deepEqual(item.metadata?.allow_for_session_scope, {
    type: "exact_file_set",
    files_hash: "a".repeat(64),
    file_count: 1,
    files: ["README.md"]
  });
});

test("ChatTranscript applies backend approval result events", () => {
  const transcript = new ChatTranscript();
  transcript.applyEvent(event({
    sequence: 1,
    type: "prompt_for_input",
    payload: {
      kind: "approval",
      approval_id: "approval-1",
      reason: "write file"
    }
  }));
  transcript.applyEvent(event({
    sequence: 2,
    type: "prompt_for_input",
    payload: {
      kind: "approval_result",
      approval_id: "approval-1",
      metadata: {
        status: "expired",
        allow: false,
        allow_for_session: false
      }
    }
  }));

  const state = transcript.state();
  assert.equal(state.approvals.length, 0);
  assert.equal(state.items[0].status, "expired");
  assert.equal(state.items[0].text.includes("expired and denied"), true);
});

test("ChatTranscript clears stale session and job state", () => {
  const transcript = new ChatTranscript();
  transcript.setSession({ session_id: "session-1", workspace_root: "/workspace", mode: "readonly" });
  transcript.setJob("job-1", "running");

  transcript.clearSession("closed");

  const state = transcript.state();
  assert.equal(state.sessionId, null);
  assert.equal(state.jobId, null);
  assert.equal(state.jobStatus, "closed");
});

test("ChatTranscript expires approvals when their session is detached", () => {
  const transcript = new ChatTranscript();
  transcript.setSession({ session_id: "session-1", workspace_root: "/workspace", mode: "review" });
  transcript.applyEvent(event({
    sequence: 1,
    type: "prompt_for_input",
    payload: { kind: "approval", approval_id: "approval-1", reason: "write file" }
  }));

  transcript.clearSession("bridge disconnected");

  const state = transcript.state();
  assert.equal(state.approvals.length, 0);
  assert.equal(state.items[0].status, "expired");
  assert.equal(state.items[0].text.includes("expired and denied"), true);
});

test("ChatTranscript reset starts a genuinely blank conversation", () => {
  const transcript = new ChatTranscript();
  transcript.setSession({ session_id: "session-1", workspace_root: "/workspace", mode: "review" });
  transcript.setJob("job-1", "running");
  transcript.addUserMessage("old conversation");
  transcript.markReplayTruncated();
  transcript.setArtifacts([{ artifact_id: "session:a.txt", root: "session", path: "a.txt", size_bytes: 1 }], true);

  transcript.reset("new session");

  assert.deepEqual(transcript.state(), {
    sessionId: null,
    jobId: null,
    workspaceRoot: null,
    mode: null,
    jobStatus: "new session",
    lastSequence: 0,
    replayTruncated: false,
    items: [],
    itemsTrimmed: false,
    approvals: [],
    artifacts: [],
    artifactsTruncated: false
  });
});

test("ChatTranscript keeps user-visible failures distinct from neutral notices", () => {
  const transcript = new ChatTranscript();

  transcript.addError("Provider subscription is not configured.");

  const [item] = transcript.state().items;
  assert.equal(item.kind, "error");
  // The raw backend sentence stays as the item body for diagnostics ...
  assert.equal(item.text, "Provider subscription is not configured.");
  // ... while the card the user reads is the classified projection.
  assert.equal(item.error?.kind, "unknown");
  assert.equal(item.title, "Alysis Code could not finish that");
  assert.equal(item.detail, "Provider subscription is not configured.");
  assert.deepEqual(item.actions?.map((action) => action.command), ["alysis.showBridgeHealth"]);
});

test("ChatTranscript classifies an expired API key into an actionable card without protocol codes", () => {
  const transcript = new ChatTranscript();

  transcript.addError("Request failed with status 401: invalid api key (auth_error)", {
    kind: "auth_invalid",
    title: "Your API key was rejected",
    detail: "The provider refused the stored key, so nothing was sent for this task.",
    actions: [{ label: "Update API key", command: "alysis.configureProvider", primary: true }]
  });

  const [item] = transcript.state().items;
  assert.equal(item.error?.kind, "auth_invalid");
  assert.equal(item.title, "Your API key was rejected");
  assert.doesNotMatch(item.title ?? "", /401|auth_error/);
  assert.doesNotMatch(item.detail ?? "", /401|auth_error/);
  assert.deepEqual(item.actions, [
    { label: "Update API key", command: "alysis.configureProvider", primary: true }
  ]);
  // Diagnostics are not lost: the raw text remains the item body.
  assert.match(item.text, /401/);
});

test("ChatTranscript classifies a failed job status instead of pasting the backend error", () => {
  const transcript = new ChatTranscript();

  transcript.setJobStatus({
    job_id: "job-1",
    session_id: "session-1",
    status: "failed",
    error: "Rate limit reached for this model (429). Retry after 30 seconds."
  } as never);

  const [item] = transcript.state().items;
  assert.equal(item.kind, "error");
  assert.equal(item.error?.kind, "rate_limited");
  assert.equal(item.retryAfterSeconds, 30);
  assert.doesNotMatch(item.title ?? "", /429/);
  assert.doesNotMatch(item.detail ?? "", /429/);
});

test("ChatTranscript renders a backend error_raised event without its protocol code as the title", () => {
  const transcript = new ChatTranscript();
  transcript.setSession({ session_id: "session-1", workspace_root: "/workspace", mode: "readonly" });

  transcript.applyEvent(event({
    sequence: 1,
    type: "error_raised",
    payload: { code: "invalid_api_key", message: "The configured API key was rejected." }
  }));

  const [item] = transcript.state().items;
  assert.equal(item.kind, "error");
  assert.equal(item.title, "Your API key was rejected");
  assert.equal(item.error?.kind, "auth_invalid");
  assert.equal(item.error?.actions[0]?.command, "alysis.configureProvider");
  assert.equal(item.error?.actions[0]?.primary, true);
});

test("ChatTranscript tracks event sequences independently per session", () => {
  const transcript = new ChatTranscript();
  transcript.setSession({ session_id: "session-a", workspace_root: "/workspace", mode: "readonly" });
  transcript.applyEvent(event({
    session_id: "session-a",
    sequence: 5,
    type: "message_end",
    payload: { text: "from session A" }
  }));
  assert.equal(transcript.lastSequence(), 5);
  transcript.clearSession("closed");

  transcript.setSession({ session_id: "session-b", workspace_root: "/workspace", mode: "readonly" });
  transcript.applyEvent(event({
    session_id: "session-b",
    sequence: 1,
    type: "message_end",
    payload: { text: "from session B" }
  }));
  transcript.applyEvent(event({
    session_id: "session-b",
    sequence: 1,
    type: "message_end",
    payload: { text: "duplicate session B" }
  }));

  const state = transcript.state();
  assert.equal(state.lastSequence, 1);
  assert.equal(transcript.lastSequence("session-a"), 5);
  assert.equal(state.items.some((item) => item.text === "from session B"), true);
  assert.equal(state.items.some((item) => item.text === "duplicate session B"), false);
});

for (const eventFirst of [true, false]) {
  test(`ChatTranscript merges the same job failure from stream and status (${eventFirst ? "event" : "status"} first)`, () => {
    const transcript = new ChatTranscript();
    const message = "OpenAI Responses stream error: You have no credits remaining.";
    const status = { job_id: "job-1", session_id: "session-1", status: "failed", error: message };
    const applyError = () => transcript.applyEvent(event({ type: "error_raised", payload: { message } }));
    if (eventFirst) {
      applyError();
      transcript.setJobStatus(status);
    } else {
      transcript.setJobStatus(status);
      applyError();
    }
    transcript.setJobStatus(status);
    const [item] = transcript.state().items;
    assert.equal(transcript.state().items.length, 1);
    assert.equal(item.error?.kind, "quota_exhausted");
    assert.equal(item.text, message);
    assert.equal(item.metadata?.sequence, 1);
    assert.equal(item.timestamp, "2026-05-19T10:00:00.000Z");
  });
}

test("ChatTranscript keeps distinct job failures and later turns visible", () => {
  const transcript = new ChatTranscript();
  const status = { job_id: "job-1", session_id: "session-1", status: "failed", error: "First failure" };
  transcript.setJobStatus(status);
  transcript.setJobStatus({ ...status, error: "Different failure" });
  transcript.setJobStatus({ ...status, job_id: "job-2" });
  transcript.setJobStatus({ ...status, session_id: "session-2" });
  transcript.addError(status.error);
  assert.equal(transcript.state().items.length, 5);
  transcript.reset();
  transcript.setJobStatus(status);
  assert.equal(transcript.state().items.length, 1);
});

test("ChatTranscript preserves structural error classification across a later status poll", () => {
  const transcript = new ChatTranscript();
  const status = { job_id: "job-1", session_id: "session-1", status: "failed", error: "Request failed" };
  transcript.setJobStatus(status);
  transcript.applyEvent(event({ type: "error_raised", payload: { code: "insufficient_quota", message: status.error } }));
  transcript.setJobStatus(status);
  assert.equal(transcript.state().items.length, 1);
  assert.equal(transcript.state().items[0].error?.kind, "quota_exhausted");
});

test("ChatTranscript surfaces a failure again when the original card was trimmed", () => {
  const transcript = new ChatTranscript();
  const status = { job_id: "job-1", session_id: "session-1", status: "failed", error: "Request failed" };
  transcript.setJobStatus(status);
  for (let index = 0; index < 200; index += 1) {
    transcript.addNotice(`notice ${index}`);
  }
  transcript.setJobStatus(status);
  assert.equal(transcript.state().items.at(-1)?.kind, "error");
});

test("ChatTranscript records replay truncation and artifacts", () => {
  const transcript = new ChatTranscript();
  transcript.markReplayTruncated();
  transcript.setArtifacts([{ artifact_id: "session:a.txt", root: "session", path: "a.txt", size_bytes: 1 }], false);

  const state = transcript.state();
  assert.equal(state.replayTruncated, true);
  assert.equal(state.artifacts[0].path, "a.txt");
  assert.equal(state.items[0].text.includes("truncated"), true);
});

test("ChatTranscript caps chat history and flags trimming", () => {
  const transcript = new ChatTranscript();
  for (let i = 0; i < 260; i += 1) {
    transcript.addUserMessage(`message ${i}`);
  }
  const state = transcript.state();
  // Capped to the 200 most-recent items; oldest trimmed, newest retained.
  assert.equal(state.items.length, 200);
  assert.equal(state.itemsTrimmed, true);
  assert.equal(state.items[state.items.length - 1].text, "message 259");
  assert.equal(state.items[0].text, "message 60");
});

test("ChatTranscript prunes the tool map on trim so a late completion is not lost", () => {
  const transcript = new ChatTranscript();
  transcript.applyEvent(event({ sequence: 1, type: "tool_call_started", payload: { call_id: "c1", name: "fs_read", arguments_preview: "x" } }));
  for (let i = 0; i < 220; i += 1) {
    transcript.addUserMessage(`m${i}`); // trims the tool item out of items[]
  }
  // The late completion for the now-trimmed tool must surface a fresh item, not mutate a detached one.
  transcript.applyEvent(event({ sequence: 2, type: "tool_call_completed", payload: { call_id: "c1", success: true, result_preview: "done" } }));
  const state = transcript.state();
  assert.equal(state.items.some((item) => item.text === "done"), true);
});

test("ChatTranscript does not flag trimming below the cap", () => {
  const transcript = new ChatTranscript();
  transcript.addUserMessage("hello");
  const state = transcript.state();
  assert.equal(state.items.length, 1);
  assert.equal(state.itemsTrimmed, false);
});

test("ChatTranscript keeps heap and visible state bounded across 100,000 events", () => {
  const transcript = new ChatTranscript();
  transcript.setSession({ session_id: "stress-session", workspace_root: "/workspace", mode: "readonly" });
  const heapBefore = process.memoryUsage().heapUsed;

  for (let sequence = 1; sequence <= 100_000; sequence += 1) {
    transcript.applyEvent(event({
      session_id: "stress-session",
      sequence,
      type: "warning_emitted",
      payload: { message: `Heads up ${sequence % 100}` }
    }));
  }

  const state = transcript.state();
  const heapGrowth = process.memoryUsage().heapUsed - heapBefore;
  assert.equal(state.lastSequence, 100_000);
  assert.equal(state.items.length, 200);
  assert.equal(state.itemsTrimmed, true);
  assert.ok(heapGrowth < 128 * 1024 * 1024, `heap growth was ${heapGrowth} bytes`);
});

test("ChatTranscript one-hour-equivalent lifecycle churn does not retain sessions", () => {
  const transcript = new ChatTranscript();
  const heapBefore = process.memoryUsage().heapUsed;

  // One cycle represents a minute of an active task: start, 60 status samples,
  // completion, and a clean reset. Sixty cycles exercise one hour of lifecycle churn
  // without forcing the test runner to sleep for wall-clock time.
  for (let minute = 0; minute < 60; minute += 1) {
    const sessionId = `hour-session-${minute}`;
    transcript.setSession({ session_id: sessionId, workspace_root: "/workspace", mode: "review" });
    transcript.setJob(`job-${minute}`, "running");
    for (let second = 1; second <= 60; second += 1) {
      transcript.applyEvent({
        ...event({
          sequence: minute * 60 + second,
          type: "status_update",
          payload: { status: "running" }
        }),
        session_id: sessionId,
        job_id: `job-${minute}`
      });
    }
    transcript.reset();
  }

  const state = transcript.state();
  const heapGrowth = process.memoryUsage().heapUsed - heapBefore;
  assert.equal(state.sessionId, null);
  assert.equal(state.items.length, 0);
  assert.equal(state.lastSequence, 0);
  assert.ok(heapGrowth < 64 * 1024 * 1024, `heap growth was ${heapGrowth} bytes`);
});

test("ChatTranscript keeps bridge job-lifecycle warnings out of the conversation", () => {
  const transcript = new ChatTranscript();
  transcript.setSession({ session_id: "session-1", workspace_root: "/workspace", mode: "review" });
  transcript.applyEvent(event({ sequence: 1, type: "warning_emitted", payload: { message: "cancellation_requested 27169b7c reason=cancelled_by_user" } }));
  transcript.applyEvent(event({ sequence: 2, type: "warning_emitted", payload: { message: "job_cancelled 27169b7c reason=cancelled_by_user" } }));
  transcript.applyEvent(event({ sequence: 4, type: "warning_emitted", payload: { message: "Checkpoint storage is still initializing; continuing without checkpoints." } }));
  transcript.applyEvent(event({ sequence: 5, type: "warning_emitted", payload: { message: "prompt_recovery_stopped 27169b7c reason=interrupted_indeterminate" } }));

  const items = transcript.state().items;
  assert.deepEqual(items.map((item) => item.kind), ["warning"]);
  assert.match(items[0].text, /Checkpoint storage/);
});

test("ChatTranscript renders one calm notice when a job ends cancelled", () => {
  const transcript = new ChatTranscript();
  transcript.setSession({ session_id: "session-1", workspace_root: "/workspace", mode: "review" });
  transcript.setJob("job-1", "running");
  transcript.setJobStatus({ job_id: "job-1", session_id: "session-1", status: "cancellation_requested", state: "cancellation_requested", cancellable: false });
  transcript.setJobStatus({ job_id: "job-1", session_id: "session-1", status: "cancelled", state: "cancelled", cancellable: false });
  transcript.setJobStatus({ job_id: "job-1", session_id: "session-1", status: "cancelled", state: "cancelled", cancellable: false });

  const notices = transcript.state().items.filter((item) => item.kind === "notice");
  assert.equal(notices.length, 1);
  assert.equal(notices[0].text, "Task stopped at your request.");
});

test("ChatTranscript flags the turn-spanning activity so the sidebar can hide it from the step list", () => {
  const transcript = new ChatTranscript();
  transcript.applyEvent(event({
    sequence: 1,
    type: "activity_update",
    payload: { activity_id: "prompt-1", kind: "plan", operation: "execute_prompt", display_title: "Working on request", status: "running" }
  }));
  transcript.applyEvent(event({
    sequence: 2,
    type: "activity_update",
    payload: { activity_id: "c1", kind: "read", operation: "read_file", display_title: "Read file", status: "running", target: "src/app.ts" }
  }));

  const items = transcript.state().items;
  assert.equal(items[0].metadata?.turnLevel, true);
  assert.equal(items[1].metadata?.turnLevel, false);
  assert.equal(JSON.stringify(items).includes("execute_prompt"), false);
});

function event(overrides: Partial<ProtocolEventEnvelope>): ProtocolEventEnvelope {
  return {
    protocol_version: PROTOCOL_VERSION,
    session_id: "session-1",
    run_id: null,
    job_id: "job-1",
    sequence: 1,
    timestamp: "2026-05-19T10:00:00.000Z",
    type: "message_delta",
    payload: {},
    ...overrides
  };
}

test("ChatTranscript follows backend mode changes without carding them", () => {
  const transcript = new ChatTranscript();
  transcript.setSession({ session_id: "session-1", workspace_root: "/workspace", mode: "review" });
  transcript.applyEvent(event({ sequence: 1, type: "mode_changed", payload: { mode: "auto" } }));
  transcript.applyEvent(event({ sequence: 2, type: "status_update", payload: { mode: "auto", model: "gpt-5" } }));

  const state = transcript.state();
  assert.equal(state.mode, "auto");
  assert.equal(state.items.length, 0);
});

test("ChatTranscript ignores empty notices", () => {
  const transcript = new ChatTranscript();
  transcript.addNotice("");
  transcript.addNotice("   ");
  assert.equal(transcript.state().items.length, 0);
});
