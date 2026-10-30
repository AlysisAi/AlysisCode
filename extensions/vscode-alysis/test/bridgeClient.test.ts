import assert from "node:assert/strict";
import { EventEmitter, once } from "node:events";
import path from "node:path";
import { PassThrough } from "node:stream";
import test from "node:test";

import {
  BridgeProcess,
  ProtocolClientError,
  AlysisBridgeClient,
  requestTimeoutForMethod,
  taskkillExecutablePath
} from "../src/client/AlysisBridgeClient";
import { CliDiscovery } from "../src/client/CliDiscovery";
import {
  MANAGEMENT_BRIDGE_METHODS,
  PROTOCOL_VERSION,
  ProtocolEventEnvelope,
  REQUIRED_BRIDGE_METHODS
} from "../src/client/AlysisProtocol";

test("AlysisBridgeClient correlates JSONL responses by request id", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.startStdio(config());
  assert.equal(fake.initializeRequests, 1);
  assert.equal(client.supportsEvent("message_delta"), true);
  assert.equal(client.supportsEvent("activity_update"), false);

  const first = client.sessionStatus("session-1");
  const second = client.sessionUsage("session-1");
  await fake.waitForRequests(2);

  fake.respond(1, {
    session_id: "session-1",
    by_model: [{ model: "test-model" }],
    totals: { calls: 1 },
    call_count: 1
  });
  fake.respond(0, sessionStatusPayload({ model: "correlated-model" }));

  assert.equal((await first).model, "correlated-model");
  assert.equal((await second).call_count, 1);
  client.dispose();
});

test("turn question responses must match the requested session and job", async () => {
  const fake = new FakeBridgeProcess({ initializePayload: healthPayloadWithMethods([
    "session.userQuestions.get", "session.userQuestions.answer", "session.userQuestions.touch"
  ]) });
  const client = clientFor(fake);
  await client.startStdio(config());
  try {
    const pending = client.sessionUserQuestions("s", "j");
    await fake.waitForRequests(1);
    fake.respond(0, { session_id: "s", job_id: "j", questions: [{ request_id: "q", status: "pending" }] });
    assert.equal((await pending).questions[0].request_id, "q");
    const answer = client.answerSessionUserQuestions("s", "j", "q", [{ id: "a", selected: [], text: "Proceed" }]);
    await fake.waitForRequests(2);
    assert.equal(fake.requests[1].params.request_id, "q");
    fake.respond(1, { session_id: "s", job_id: "other-job", questions: [] });
    await assert.rejects(answer, (error: any) => error?.code === "invalid_response");
    const touch = client.touchSessionUserQuestions("s", "j", "q");
    await fake.waitForRequests(3);
    fake.respond(2, { session_id: "other-session", job_id: "j", questions: [] });
    await assert.rejects(touch, (error: any) => error?.code === "invalid_response");
  } finally { client.dispose(); }
});

test("host dispatch acknowledgement cannot resolve a different action", async () => {
  const fake = new FakeBridgeProcess({ initializePayload: healthPayloadWithMethods(["host.action.dispatched"]) });
  const client = clientFor(fake);
  await client.startStdio(config());
  const params = { session_id: "s", host_action_id: `ha_${"a".repeat(32)}`,
    workspace_fence: "wf_fixture", capability_fingerprint: "b".repeat(64) };
  try {
    const pending = client.hostActionDispatched(params);
    await fake.waitForRequests(1);
    assert.deepEqual(fake.requests[0].params, params);
    fake.respond(0, { status: "dispatched", host_action_id: params.host_action_id });
    assert.equal((await pending).status, "dispatched");
    const wrong = client.hostActionDispatched(params);
    await fake.waitForRequests(2);
    fake.respond(1, { status: "dispatched", host_action_id: `ha_${"c".repeat(32)}` });
    await assert.rejects(wrong, (error: any) => error?.code === "invalid_response");
  } finally { client.dispose(); }
});

test("AlysisBridgeClient correlates twelve concurrent in-flight requests", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.startStdio(config());

  const pending = Array.from({ length: 12 }, (_, index) =>
    client.sessionStatus(`session-${index}`).then((status) => ({ index, status }))
  );
  await fake.waitForRequests(12);

  for (let index = 11; index >= 0; index -= 1) {
    fake.respond(index, sessionStatusPayload({
      session_id: `session-${index}`,
      model: `concurrent-model-${index}`
    }));
  }

  const resolved = await Promise.all(pending);
  assert.deepEqual(
    resolved.map(({ index, status }) => [status.session_id, status.model, index]),
    Array.from({ length: 12 }, (_, index) => [
      `session-${index}`,
      `concurrent-model-${index}`,
      index
    ])
  );
  client.dispose();
});

test("AlysisBridgeClient serializes concurrent startup and waits for one initialize handshake", async () => {
  const fake = new FakeBridgeProcess({ deferInitialize: true });
  let processFactoryCalls = 0;
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    () => {
      processFactoryCalls += 1;
      return fake;
    }
  );
  let firstReady = false;
  let secondReady = false;

  const first = client.ensureStarted(config()).then(() => {
    firstReady = true;
  });
  const second = client.ensureStarted(config()).then(() => {
    secondReady = true;
  });

  await fake.waitForInitializeRequests(1);
  assert.equal(processFactoryCalls, 1);
  assert.equal(fake.initializeRequests, 1);
  assert.equal(client.isRunning(), false);
  assert.equal(firstReady, false);
  assert.equal(secondReady, false);

  fake.respondToInitialize();
  await Promise.all([first, second]);

  assert.equal(client.isRunning(), true);
  assert.equal(firstReady, true);
  assert.equal(secondReady, true);
  client.dispose();
});

test("AlysisBridgeClient single-flights twelve concurrent startup callers", async () => {
  const fake = new FakeBridgeProcess({ deferInitialize: true });
  let processFactoryCalls = 0;
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    () => {
      processFactoryCalls += 1;
      return fake;
    }
  );

  const callers = Array.from({ length: 12 }, () => client.ensureStarted(config()));
  await fake.waitForInitializeRequests(1);

  assert.equal(processFactoryCalls, 1);
  assert.equal(fake.initializeRequests, 1);
  fake.respondToInitialize();
  await Promise.all(callers);
  assert.equal(client.isRunning(), true);
  client.dispose();
});

test("AlysisBridgeClient routes structured events and tracks sequence", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.startStdio(config());

  const eventPromise = once(client, "event") as Promise<[ProtocolEventEnvelope]>;
  const sessionEventPromise = once(client, "session:session-1") as Promise<[ProtocolEventEnvelope]>;
  const jobEventPromise = once(client, "job:job-1") as Promise<[ProtocolEventEnvelope]>;
  fake.emitEvent(eventEnvelope({ sequence: 7, type: "message_delta", payload: { text: "hello" } }));
  const [event] = await eventPromise;
  const [sessionEvent] = await sessionEventPromise;
  const [jobEvent] = await jobEventPromise;

  assert.equal(event.session_id, "session-1");
  assert.equal(sessionEvent.sequence, 7);
  assert.equal(jobEvent.sequence, 7);
  assert.equal(client.lastSequence("session-1"), 7);
  client.dispose();
});

test("AlysisBridgeClient handles approval allow and deny responses", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.startStdio(config());

  const allow = client.respondApproval({
    session_id: "session-1",
    approval_id: "approval-1",
    allow: true,
    allow_for_session: false
  });
  await fake.waitForRequests(1);
  assert.equal(fake.requests[0].method, "approval.respond");
  assert.equal(fake.requests[0].params.allow, true);
  fake.respond(0, {
    session_id: "session-1",
    approval_id: "approval-1",
    status: "resolved",
    allow: true,
    allow_for_session: false,
    allow_for_session_supported: false,
    allow_for_session_scope: null,
    allow_for_session_warning: null
  });
  assert.equal((await allow).status, "resolved");

  const deny = client.respondApproval({
    session_id: "session-1",
    approval_id: "approval-2",
    allow: false,
    allow_for_session: false
  });
  await fake.waitForRequests(2);
  assert.equal(fake.requests[1].params.allow, false);
  fake.respond(1, {
    session_id: "session-1",
    approval_id: "approval-2",
    status: "resolved",
    allow: false,
    allow_for_session: false,
    allow_for_session_supported: false,
    allow_for_session_scope: null,
    allow_for_session_warning: null
  });
  assert.equal((await deny).allow, false);
  client.dispose();
});

test("AlysisBridgeClient sends run/chat parity params and parses session methods", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.startStdio(config());

  const create = client.createSession({
    workspace: "/workspace",
    mode: "review",
    model: "override-model",
    base_url: "https://provider.example/v1",
    temperature: 0.4,
    stream: true,
    verify_commands: ["pytest -q"],
    subagents_enabled: false,
    no_log: true,
    yes: true,
    max_steps: 7,
    active_workdir: "src"
  });
  await fake.waitForRequests(1);
  assert.equal(fake.requests[0].method, "session.create");
  assert.equal(fake.requests[0].params.temperature, 0.4);
  assert.deepEqual(fake.requests[0].params.verify_commands, ["pytest -q"]);
  assert.equal(fake.requests[0].params.subagents_enabled, false);
  fake.respond(0, { session_id: "session-1", workspace_root: "/workspace", mode: "review" });
  assert.equal((await create).session_id, "session-1");

  const chat = client.sendChat("session-1", "inspect", { images: ["image.png"] });
  await fake.waitForRequests(2);
  assert.equal(fake.requests[1].method, "chat.send");
  assert.deepEqual(fake.requests[1].params.images, ["image.png"]);
  fake.respond(1, { session_id: "session-1", job_id: "job-1", status: "started" });
  assert.equal((await chat).job_id, "job-1");

  const run = client.startRun({
    workspace: "/workspace",
    instruction: "run",
    temperature: 0.2,
    verify_cmd: "npm test",
    image_paths: ["image.png"]
  });
  await fake.waitForRequests(3);
  assert.equal(fake.requests[2].method, "run.start");
  assert.equal(fake.requests[2].params.verify_cmd, "npm test");
  fake.respond(2, { session_id: "session-2", job_id: "job-2", status: "started" });
  assert.equal((await run).session_id, "session-2");

  const status = client.sessionStatus("session-1");
  await fake.waitForRequests(4);
  assert.equal(fake.requests[3].method, "session.status");
  fake.respond(3, sessionStatusPayload({ stream: true }));
  assert.equal((await status).stream, true);

  const usage = client.sessionUsage("session-1");
  await fake.waitForRequests(5);
  assert.equal(fake.requests[4].method, "session.usage");
  fake.respond(4, {
    session_id: "session-1",
    by_model: [{ model: "test-model" }],
    totals: { calls: 1 },
    call_count: 1
  });
  assert.equal((await usage).call_count, 1);

  const history = client.sessionHistory("session-1", "needle", { max_results: 3 });
  await fake.waitForRequests(6);
  assert.equal(fake.requests[5].method, "session.history");
  assert.equal(fake.requests[5].params.max_results, 3);
  fake.respond(5, {
    session_id: "session-1",
    pattern: "needle",
    matches: [{ kind: "history", path: "history/chunk.jsonl", line: 1, text: "needle" }],
    truncated: false
  });
  assert.equal((await history).matches[0].text, "needle");

  const context = client.sessionContext("session-1");
  await fake.waitForRequests(7);
  assert.equal(fake.requests[6].method, "session.context");
  fake.respond(6, {
    session_id: "session-1",
    model_name: "test-model",
    max_input_tokens: 100,
    used_input_tokens: 10,
    remaining_tokens: 90,
    percent_left: 90,
    source: "test",
    message_count: 2,
    pinned_prefix_len: 1
  });
  assert.equal((await context).remaining_tokens, 90);

  const compact = client.sessionCompact("session-1", "needle");
  await fake.waitForRequests(8);
  assert.equal(fake.requests[7].method, "session.compact");
  fake.respond(7, {
    session_id: "session-1",
    changed: true,
    focus: "needle",
    tokens_before: 100,
    tokens_after: 50,
    tokens_delta: -50,
    message_count: 2
  });
  assert.equal((await compact).changed, true);

  const resume = client.sessionResume("session-1", "previous-session");
  await fake.waitForRequests(9);
  assert.equal(fake.requests[8].method, "session.resume");
  fake.respond(8, {
    session_id: "session-1",
    resumed_session_id: "previous-session",
    resumed: true,
    message: "resumed",
    history_count: 4,
    bounded: true,
    source: "retained_session_log_replay"
  });
  assert.equal((await resume).history_count, 4);

  const imagesList = client.sessionImagesList("session-1");
  await fake.waitForRequests(10);
  assert.equal(fake.requests[9].method, "session.images.list");
  fake.respond(9, {
    session_id: "session-1",
    images: [{ path: "/workspace/image.png", relpath: "image.png", mime_type: "image/png", size_bytes: 8 }],
    count: 1,
    max_bytes: 26214400,
    binary_jsonl: false
  });
  assert.equal((await imagesList).images[0].relpath, "image.png");

  const imagesAdd = client.sessionImagesAdd({ session_id: "session-1", images: ["image.png"] });
  await fake.waitForRequests(11);
  assert.equal(fake.requests[10].method, "session.images.add");
  assert.deepEqual(fake.requests[10].params.images, ["image.png"]);
  fake.respond(10, {
    session_id: "session-1",
    images: [{ path: "/workspace/image.png", relpath: "image.png", mime_type: "image/png", size_bytes: 8 }],
    count: 1,
    max_bytes: 26214400,
    binary_jsonl: false,
    added_count: 1,
    replaced: false
  });
  assert.equal((await imagesAdd).added_count, 1);

  const imagesClear = client.sessionImagesClear("session-1");
  await fake.waitForRequests(12);
  assert.equal(fake.requests[11].method, "session.images.clear");
  fake.respond(11, {
    session_id: "session-1",
    cleared: true,
    count_before: 1,
    count_after: 0,
    images: []
  });
  assert.equal((await imagesClear).count_before, 1);

  const mode = client.sessionSetMode("session-1", "readonly");
  await fake.waitForRequests(13);
  assert.equal(fake.requests[12].method, "session.setMode");
  fake.respond(12, sessionStatusPayload({ mode: "readonly" }));
  assert.equal((await mode).mode, "readonly");

  const model = client.sessionSetModel("session-1", "next-model", {
    base_url: "https://provider.example/v1"
  });
  await fake.waitForRequests(14);
  assert.equal(fake.requests[13].method, "session.setModel");
  fake.respond(13, sessionStatusPayload({ model: "next-model" }));
  assert.equal((await model).model, "next-model");

  const stream = client.sessionSetStream("session-1", false);
  await fake.waitForRequests(15);
  assert.equal(fake.requests[14].method, "session.setStream");
  fake.respond(14, sessionStatusPayload({ stream: false }));
  assert.equal((await stream).stream, false);

  const workdir = client.sessionSetActiveWorkdir("session-1", "src");
  await fake.waitForRequests(16);
  assert.equal(fake.requests[15].method, "session.setActiveWorkdir");
  fake.respond(15, {
    session_id: "session-1",
    source: "ide_protocol",
    workspace_root: "/workspace",
    previous_active_workdir: "/workspace",
    previous_active_workdir_relpath: ".",
    active_workdir: "/workspace/src",
    active_workdir_relpath: "src",
    changed: true
  });
  assert.equal((await workdir).active_workdir_relpath, "src");

  const clear = client.sessionClear("session-1");
  await fake.waitForRequests(17);
  assert.equal(fake.requests[16].method, "session.clear");
  fake.respond(16, {
    session_id: "session-1",
    cleared: true,
    messages_before: 8,
    messages_after: 1
  });
  assert.equal((await clear).messages_after, 1);

  const modelInfo = client.sessionModelInfo("session-1", { model: "explicit-model" });
  await fake.waitForRequests(18);
  assert.equal(fake.requests[17].method, "session.modelInfo");
  assert.deepEqual(fake.requests[17].params, {
    session_id: "session-1",
    model: "explicit-model"
  });
  fake.respond(17, {
    session_id: "session-1",
    model: "explicit-model",
    provider: "openai",
    profile: "demo",
    base_url: "https://<redacted>@provider.example/v1",
    base_url_redacted: true,
    context_window: 128000,
    vision_support: true,
    tool_support: true,
    streaming_support: true,
    source: "config",
    source_metadata: { config: true },
    secret_values_included: false
  });
  assert.equal((await modelInfo).base_url_redacted, true);

  const subagentStatus = client.sessionSubagentsStatus("session-1");
  await fake.waitForRequests(19);
  assert.equal(fake.requests[18].method, "session.subagents.status");
  fake.respond(18, {
    session_id: "session-1",
    enabled: false,
    available: ["planner"],
    available_count: 1,
    explicit_execution_supported: false,
    explicit_execution_policy: "status/toggle only",
    lifecycle_event: "subagent_state_changed",
    execution_lifecycle: "in_turn_parent_owned",
    cancellation: "parent_job",
    independently_resumable: false,
    background_worker_surface: "forge.swarm",
    forge_execute_policy: "disabled",
    secret_values_included: false
  });
  const parsedSubagentStatus = await subagentStatus;
  assert.equal(parsedSubagentStatus.available[0], "planner");
  assert.equal(parsedSubagentStatus.lifecycle_event, "subagent_state_changed");
  assert.equal(parsedSubagentStatus.execution_lifecycle, "in_turn_parent_owned");
  assert.equal(parsedSubagentStatus.cancellation, "parent_job");
  assert.equal(parsedSubagentStatus.independently_resumable, false);
  assert.equal(parsedSubagentStatus.background_worker_surface, "forge.swarm");

  const subagentToggle = client.sessionSubagentsSetEnabled("session-1", true, true);
  await fake.waitForRequests(20);
  assert.equal(fake.requests[19].method, "session.subagents.setEnabled");
  assert.deepEqual(fake.requests[19].params, {
    session_id: "session-1",
    enabled: true,
    workspace_trusted: true
  });
  fake.respond(19, {
    session_id: "session-1",
    enabled: true,
    available: ["planner"],
    available_count: 1,
    explicit_execution_supported: false,
    explicit_execution_policy: "status/toggle only",
    lifecycle_event: "subagent_state_changed",
    execution_lifecycle: "in_turn_parent_owned",
    cancellation: "parent_job",
    independently_resumable: false,
    background_worker_surface: "forge.swarm",
    forge_execute_policy: "disabled",
    secret_values_included: false,
    changed: true,
    previous_enabled: false
  });
  assert.equal((await subagentToggle).changed, true);

  const traceStatus = client.sessionTraceStatus("session-1");
  await fake.waitForRequests(21);
  assert.equal(fake.requests[20].method, "session.trace.status");
  fake.respond(20, {
    session_id: "session-1",
    supported: true,
    level: "compact",
    levels: ["off", "compact", "full"],
    retained_events: 2,
    redacted: true,
    secret_values_included: false,
    max_events: 500,
    max_bytes: 262144,
    full_trace_requires_confirmation: true
  });
  assert.equal((await traceStatus).level, "compact");

  const traceFull = client.sessionTraceSetLevel("session-1", "full", { confirm: true });
  await fake.waitForRequests(22);
  assert.equal(fake.requests[21].method, "session.trace.setLevel");
  assert.deepEqual(fake.requests[21].params, {
    session_id: "session-1",
    level: "full",
    confirm: true
  });
  fake.respond(21, {
    session_id: "session-1",
    supported: true,
    level: "full",
    levels: ["off", "compact", "full"],
    redacted: true,
    secret_values_included: false,
    max_events: 500,
    max_bytes: 262144,
    full_trace_requires_confirmation: true,
    changed: true,
    previous_level: "compact"
  });
  assert.equal((await traceFull).previous_level, "compact");

  const traceEvents = client.sessionTraceListEvents("session-1", { max_events: 5, max_bytes: 1000 });
  await fake.waitForRequests(23);
  assert.equal(fake.requests[22].method, "session.trace.listEvents");
  assert.deepEqual(fake.requests[22].params, {
    session_id: "session-1",
    max_events: 5,
    max_bytes: 1000
  });
  fake.respond(22, {
    session_id: "session-1",
    level: "full",
    events: [{ protocol_version: "1", session_id: "session-1", sequence: 1, timestamp: "now", type: "info_emitted", payload: { message: "ok" } }],
    count: 1,
    total_retained: 1,
    bytes: 120,
    redacted: true,
    secret_values_included: false,
    max_events: 5,
    max_bytes: 1000,
    truncated: false,
    lowest_retained_sequence: 1,
    highest_retained_sequence: 1
  });
  assert.equal((await traceEvents).events[0].type, "info_emitted");

  const traceArtifact = client.sessionTraceReadArtifact("session-1", "session:trace.txt", { max_bytes: 100 });
  await fake.waitForRequests(24);
  assert.equal(fake.requests[23].method, "session.trace.readArtifact");
  assert.deepEqual(fake.requests[23].params, {
    session_id: "session-1",
    artifact_id: "session:trace.txt",
    max_bytes: 100
  });
  fake.respond(23, {
    session_id: "session-1",
    artifact_id: "session:trace.txt",
    path: "trace.txt",
    size_bytes: 5,
    truncated: false,
    max_bytes: 100,
    encoding: "utf-8-replace",
    content: "trace",
    redacted: true,
    secret_values_included: false
  });
  assert.equal((await traceArtifact).content, "trace");

  const traceClear = client.sessionTraceClear("session-1");
  await fake.waitForRequests(25);
  assert.equal(fake.requests[24].method, "session.trace.clear");
  fake.respond(24, {
    session_id: "session-1",
    cleared: true,
    events_before: 2,
    events_after: 0,
    redacted: true,
    secret_values_included: false
  });
  assert.equal((await traceClear).events_before, 2);

  const terminalsList = client.sessionTerminalsList("session-1");
  await fake.waitForRequests(26);
  assert.equal(fake.requests[25].method, "session.terminals.list");
  fake.respond(25, {
    session_id: "session-1",
    supported: true,
    available: true,
    terminals: [{ process_id: "proc-1", cmd: "npm test", cwd: "/workspace", cwd_relpath: ".", status: "running", exit_code: null, runtime_s: 1, started_at: "now" }],
    count: 1,
    redacted: true,
    secret_values_included: false,
    arbitrary_shell_execution: false,
    interactive_pty_streaming: false
  });
  assert.equal((await terminalsList).terminals[0].process_id, "proc-1");

  const terminalShow = client.sessionTerminalsShow("session-1", "proc-1", { max_lines: 10 });
  await fake.waitForRequests(27);
  assert.equal(fake.requests[26].method, "session.terminals.show");
  assert.deepEqual(fake.requests[26].params, {
    session_id: "session-1",
    process_id: "proc-1",
    max_lines: 10
  });
  fake.respond(26, terminalShowPayload({ process_id: "proc-1" }));
  assert.equal((await terminalShow).lines[0].text, "ok");

  const terminalKill = client.sessionTerminalsKill("session-1", "proc-1", true, { confirm: true });
  await fake.waitForRequests(28);
  assert.equal(fake.requests[27].method, "session.terminals.kill");
  assert.deepEqual(fake.requests[27].params, {
    session_id: "session-1",
    process_id: "proc-1",
    workspace_trusted: true,
    confirm: true
  });
  fake.respond(27, terminalShowPayload({ process_id: "proc-1", killed: true }));
  assert.equal((await terminalKill).killed, true);

  const terminalClear = client.sessionTerminalsClear("session-1", "proc-1", true, { confirm: true });
  await fake.waitForRequests(29);
  assert.equal(fake.requests[28].method, "session.terminals.clear");
  fake.respond(28, terminalShowPayload({ process_id: "proc-1", cleared: true }));
  assert.equal((await terminalClear).cleared, true);

  client.dispose();
});

test("AlysisBridgeClient negotiates and strictly correlates VS Code host actions", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  client.setHostCapabilityProvider(() => ({
    workspace_trusted: true,
    host_capabilities: {
      protocol_version: "1",
      actions: ["tasks.list", "tasks.run", "debug.list", "debug.status"]
    }
  }));
  await client.startStdio(config());

  const negotiated = once(client, "hostActionsNegotiated");
  const create = client.createSession({ workspace: "/workspace", mode: "review" });
  await fake.waitForRequests(1);
  assert.equal(fake.requests[0].params.workspace_trusted, true);
  assert.deepEqual(fake.requests[0].params.host_capabilities, {
    protocol_version: "1",
    actions: ["tasks.list", "tasks.run", "debug.list", "debug.status"]
  });
  fake.respond(0, {
    session_id: "session-host",
    workspace_root: "/workspace",
    mode: "review",
    host_actions: {
      protocol_version: "1",
      actions: ["tasks.list", "tasks.run", "debug.list", "debug.status"],
      workspace_fence: "wf_bridge-client",
      capability_fingerprint: "a".repeat(64),
      request_event: "host_action_requested",
      cancellation_event: "host_action_cancelled",
      session_closed_event: "session_closed",
      response_method: "host.action.respond",
      request_timeout_seconds: 30,
      max_argument_bytes: 8192,
      max_result_bytes: 65536
    }
  });
  const created = await create;
  const [sessionId, workspaceRoot, hostActions] = await negotiated;
  assert.equal(created.host_actions?.workspace_fence, "wf_bridge-client");
  assert.equal(sessionId, "session-host");
  assert.equal(workspaceRoot, "/workspace");
  assert.equal(hostActions.max_result_bytes, 65536);

  // Host capabilities were negotiated by session.create above. run.start that JOINS that session
  // must carry turn params only — the bridge rejects `workspace_trusted`/`host_capabilities` on an
  // existing session as `unsupported_turn_option`.
  const run = client.startRun({ session_id: "session-host", instruction: "test" });
  await fake.waitForRequests(2);
  assert.deepEqual(fake.requests[1].params, { session_id: "session-host", instruction: "test" });
  fake.respond(1, { session_id: "session-host", job_id: "job-host", status: "started" });
  await run;

  // A session-creating run.start (no session_id) still advertises the host capabilities.
  const creatingRun = client.startRun({ workspace: "/workspace", instruction: "fresh", mode: "readonly" });
  await fake.waitForRequests(3);
  assert.equal(fake.requests[2].method, "run.start");
  assert.equal(fake.requests[2].params.workspace_trusted, true);
  assert.deepEqual(fake.requests[2].params.host_capabilities, {
    protocol_version: "1",
    actions: ["tasks.list", "tasks.run", "debug.list", "debug.status"]
  });
  fake.respond(2, { session_id: "session-fresh", job_id: "job-fresh", status: "started" });
  await creatingRun;

  const response = client.hostActionRespond({
    session_id: "session-host",
    host_action_id: `ha_${"1".repeat(32)}`,
    workspace_fence: "wf_bridge-client",
    capability_fingerprint: "a".repeat(64),
    ok: true,
    result: { tasks: [], truncated: false }
  });
  await fake.waitForRequests(4);
  assert.equal(fake.requests[3].method, "host.action.respond");
  fake.respond(3, {
    status: "applied",
    session_id: "session-host",
    host_action_id: `ha_${"1".repeat(32)}`,
    action: "tasks.list",
    outcome: "result"
  });
  assert.equal((await response).action, "tasks.list");

  const mismatched = client.hostActionRespond({
    session_id: "session-host",
    host_action_id: `ha_${"2".repeat(32)}`,
    workspace_fence: "wf_bridge-client",
    capability_fingerprint: "a".repeat(64),
    ok: false,
    error: { code: "failed", message: "failed", retryable: false }
  });
  await fake.waitForRequests(5);
  fake.respond(4, {
    status: "applied",
    session_id: "other-session",
    host_action_id: `ha_${"2".repeat(32)}`,
    action: "tasks.list",
    outcome: "error"
  });
  await assert.rejects(mismatched, (error: any) => error?.code === "invalid_response");
  client.dispose();
});

test("AlysisBridgeClient rejects malformed host action negotiation limits", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.startStdio(config());
  const create = client.createSession({ workspace: "/workspace", mode: "review" });
  await fake.waitForRequests(1);
  fake.respond(0, {
    session_id: "session-host",
    workspace_root: "/workspace",
    mode: "review",
    host_actions: {
      protocol_version: "1",
      actions: ["tasks.list"],
      workspace_fence: "wf_bridge-client",
      capability_fingerprint: "a".repeat(64),
      request_event: "host_action_requested",
      cancellation_event: "host_action_cancelled",
      session_closed_event: "session_closed",
      response_method: "host.action.respond",
      request_timeout_seconds: 30,
      max_argument_bytes: 8192,
      max_result_bytes: 65537
    }
  });
  await assert.rejects(create, (error: any) => error?.code === "invalid_response");
  client.dispose();
});

test("AlysisBridgeClient sends structured management backend methods", async () => {
  const fake = new FakeBridgeProcess({
    initializePayload: healthPayloadWithMethods([...MANAGEMENT_BRIDGE_METHODS])
  });
  const client = clientFor(fake);
  await client.startStdio(config());
  assert.equal(client.supportsMethod("config.get"), true);
  assert.equal(client.supportsMethod("report.create"), true);
  assert.equal(client.supportsMethod("mcp.status"), true);

  const workspace = "/workspace";
  const calls: Array<{
    call: () => Promise<unknown>;
    method: string;
    params: Record<string, unknown>;
    response: Record<string, unknown>;
  }> = [
    { call: () => client.configGet(), method: "config.get", params: {}, response: { config: {}, active_profile: "", api_key: {}, secret_values_included: false } },
    { call: () => client.configSet({ workspace_trusted: true, key: "model", value: "test-model" }), method: "config.set", params: { workspace_trusted: true, key: "model", value: "test-model" }, response: { key: "model", changed: true, config_path: "/config.toml", secret_values_included: false } },
    { call: () => client.configSchema(), method: "config.schema", params: {}, response: { schema: {}, secret_values_included: false } },
    { call: () => client.configValidate({ values: { model: "test-model" } }), method: "config.validate", params: { values: { model: "test-model" } }, response: { valid: true, errors: [], config: {}, secret_values_included: false } },
    { call: () => client.profileList(), method: "profile.list", params: {}, response: { active_profile: "", profiles: [], secret_values_included: false } },
    { call: () => client.profileShow({ name: "demo" }), method: "profile.show", params: { name: "demo" }, response: { profile: {}, secret_values_included: false } },
    { call: () => client.profileAdd({ workspace_trusted: true, name: "demo", base_url: "https://provider.example/v1", api_key_env: "DEMO_API_KEY" }), method: "profile.add", params: { workspace_trusted: true, name: "demo", base_url: "https://provider.example/v1", api_key_env: "DEMO_API_KEY" }, response: { changed: true, profile: {}, secret_values_included: false } },
    { call: () => client.profileRemove({ workspace_trusted: true, name: "demo", yes: true }), method: "profile.remove", params: { workspace_trusted: true, name: "demo", yes: true }, response: { changed: true, secret_values_included: false } },
    { call: () => client.profileUse({ workspace_trusted: true, name: "demo" }), method: "profile.use", params: { workspace_trusted: true, name: "demo" }, response: { active_profile: "demo", changed: true, profile: {}, secret_values_included: false } },
    { call: () => client.profileRename({ workspace_trusted: true, old: "demo", new: "renamed" }), method: "profile.rename", params: { workspace_trusted: true, old: "demo", new: "renamed" }, response: { changed: true, profile: {}, secret_values_included: false } },
    { call: () => client.profilePresets(), method: "profile.presets", params: {}, response: { presets: [], secret_values_included: false } },
    { call: () => client.profilePreset({ workspace_trusted: true, preset_key: "ollama", name: "ollama", yes: true }), method: "profile.preset", params: { workspace_trusted: true, preset_key: "ollama", name: "ollama", yes: true }, response: { changed: true, profile: {}, secret_values_included: false } },
    { call: () => client.profileConvert({ workspace_trusted: true, name: "demo", target: "native", yes: true }), method: "profile.convert", params: { workspace_trusted: true, name: "demo", target: "native", yes: true }, response: { changed: true, secret_values_included: false } },
    { call: () => client.sessionShow({ session_id: "session-1", max_events: 10 }), method: "session.show", params: { session_id: "session-1", max_events: 10 }, response: { session_id: "session-1", path: "/sessions/session-1.jsonl", events: [], event_count: 0, truncated: false, max_events: 10 } },
    { call: () => client.sessionUsage("session-1"), method: "session.usage", params: { session_id: "session-1" }, response: { session_id: "session-1", by_model: [], totals: { calls: 0 }, call_count: 0 } },
    { call: () => client.sessionScore({ latest: 1 }), method: "session.score", params: { latest: 1 }, response: { sessions_dir: "/sessions", scores: [], score: null } },
    { call: () => client.toolsCatalog(), method: "tools.catalog", params: {}, response: { tools: [], count: 0 } },
    { call: () => client.toolList({ workspace }), method: "tool.list", params: { workspace }, response: { workspace_root: workspace, tools: [] } },
    { call: () => client.toolInfo({ workspace, name: "demo" }), method: "tool.info", params: { workspace, name: "demo" }, response: { workspace_root: workspace, tools: [] } },
    { call: () => client.toolTrust({ workspace, workspace_trusted: true, name: "demo" }), method: "tool.trust", params: { workspace, workspace_trusted: true, name: "demo" }, response: { workspace_root: workspace, name: "demo", trusted: true, changed: true } },
    { call: () => client.toolUntrust({ workspace, workspace_trusted: true, name: "demo" }), method: "tool.untrust", params: { workspace, workspace_trusted: true, name: "demo" }, response: { workspace_root: workspace, name: "demo", trusted: false, changed: true } },
    { call: () => client.skillList({ workspace }), method: "skill.list", params: { workspace }, response: { workspace_root: workspace, skills: [], issues: {} } },
    { call: () => client.skillInfo({ workspace, name: "demo" }), method: "skill.info", params: { workspace, name: "demo" }, response: { workspace_root: workspace, skill: {}, info_text: "", issues: {} } },
    { call: () => client.skillInit({ workspace, workspace_trusted: true, name: "demo", project: true }), method: "skill.init", params: { workspace, workspace_trusted: true, name: "demo", project: true }, response: { changed: true, result: {} } },
    { call: () => client.skillValidate({ workspace, all: true }), method: "skill.validate", params: { workspace, all: true }, response: { workspace_root: workspace, results: [], valid: true } },
    { call: () => client.skillInstall({ workspace, workspace_trusted: true, source: "demo" }), method: "skill.install", params: { workspace, workspace_trusted: true, source: "demo" }, response: { changed: true, result: {} } },
    { call: () => client.skillEnable({ workspace, workspace_trusted: true, name: "demo" }), method: "skill.enable", params: { workspace, workspace_trusted: true, name: "demo" }, response: { workspace_root: workspace, name: "demo", enabled: true, scope: "user", changed: true } },
    { call: () => client.skillDisable({ workspace, workspace_trusted: true, name: "demo" }), method: "skill.disable", params: { workspace, workspace_trusted: true, name: "demo" }, response: { workspace_root: workspace, name: "demo", enabled: false, scope: "user", changed: true } },
    { call: () => client.skillRemove({ workspace, workspace_trusted: true, name: "demo" }), method: "skill.remove", params: { workspace, workspace_trusted: true, name: "demo" }, response: { changed: true, result: {} } },
    { call: () => client.doctorSummary(), method: "doctor.summary", params: {}, response: { python: "3.12", platform: "linux", model_set: true, api_key: {}, secret_values_included: false } },
    { call: () => client.doctorProviders(), method: "doctor.providers", params: {}, response: { diagnostics: {}, last_provider_call: {}, last_web_search: {}, secret_values_included: false } },
    { call: () => client.doctorBundle(), method: "doctor.bundle", params: {}, response: { bundle: { redacted: true }, secret_values_included: false } },
    { call: () => client.sandboxDoctor({ smoke: false }), method: "sandbox.doctor", params: { smoke: false }, response: { diagnostic: {}, message: "" } },
    { call: () => client.sandboxSetup({ workspace_trusted: true, pull: false }), method: "sandbox.setup", params: { workspace_trusted: true, pull: false }, response: { changed: false, diagnostic: {}, pull_result: null, message: "" } },
    { call: () => client.sandboxPull({ workspace_trusted: true, images: ["sandbox:latest"], timeout_s: 1 }), method: "sandbox.pull", params: { workspace_trusted: true, images: ["sandbox:latest"], timeout_s: 1 }, response: { changed: true, result: {} } },
    { call: () => client.updateCheck({ cached: true }), method: "update.check", params: { cached: true }, response: { status: {} } },
    { call: () => client.reportCreate({ workspace, workspace_trusted: true, feedback: "bug", local_only: true }), method: "report.create", params: { workspace, workspace_trusted: true, feedback: "bug", local_only: true }, response: { bundle: {}, github_issue: null, github_status_lines: [], changed: true, secret_values_included: false } },
    { call: () => client.mcpStatus({ workspace, runtime: "interactive_chat" }), method: "mcp.status", params: { workspace, runtime: "interactive_chat" }, response: { servers: [], tools: [], secret_values_included: false } },
    { call: () => client.mcpPromptsList({ workspace, server: "demo", query: "q", limit: 5, refresh: true }), method: "mcp.prompts.list", params: { workspace, server: "demo", query: "q", limit: 5, refresh: true }, response: { prompts: [], count: 0, secret_values_included: false } },
    { call: () => client.mcpPromptsGet({ workspace, server_id: "demo", prompt_name: "summarize", arguments: { topic: "repo" } }), method: "mcp.prompts.get", params: { workspace, server_id: "demo", prompt_name: "summarize", arguments: { topic: "repo" } }, response: { prompt: {}, messages: [], secret_values_included: false } },
    { call: () => client.mcpAuthStatus({ workspace, server: "demo" }), method: "mcp.auth.status", params: { workspace, server: "demo" }, response: { rows: [], secret_values_included: false } },
    { call: () => client.mcpAuthLoginStart({ workspace, workspace_trusted: true, server_id: "demo" }), method: "mcp.auth.login.start", params: { workspace, workspace_trusted: true, server_id: "demo" }, response: oauthFlowPayload({ supported: true, will_block: false, browser_opened_by_bridge: false, browser_url: "https://auth.example/authorize" }) },
    { call: () => client.mcpAuthLoginStatus({ workspace, server_id: "demo", flow_id: "flow-1" }), method: "mcp.auth.login.status", params: { workspace, server_id: "demo", flow_id: "flow-1" }, response: oauthFlowPayload() },
    { call: () => client.mcpAuthLoginCancel({ workspace, workspace_trusted: true, server_id: "demo", flow_id: "flow-1" }), method: "mcp.auth.login.cancel", params: { workspace, workspace_trusted: true, server_id: "demo", flow_id: "flow-1" }, response: oauthFlowPayload({ state: "cancelled" }) },
    { call: () => client.mcpAuthLogout({ workspace, workspace_trusted: true, server_id: "demo", yes: true }), method: "mcp.auth.logout", params: { workspace, workspace_trusted: true, server_id: "demo", yes: true }, response: { server_id: "demo", removed: true, changed: true, secret_values_included: false } },
    { call: () => client.hooksList({ workspace }), method: "hooks.list", params: { workspace }, response: { workspace_root: workspace, sources: [], hooks: [], count: 0, secret_values_included: false } },
    { call: () => client.hooksDoctor({ workspace }), method: "hooks.doctor", params: { workspace }, response: { workspace_root: workspace, sources: [], effective: {}, matcher_errors: [], untrusted_project_paths: [], secret_values_included: false } },
    { call: () => client.hooksTrace({ session_id: "session-1", limit: 10 }), method: "hooks.trace", params: { session_id: "session-1", limit: 10 }, response: { session_id: "session-1", events: [], count: 0, total_count: 0, secret_values_included: false } },
    { call: () => client.hooksTest({ workspace, event: "PreToolUse", runtime_kind: "interactive_chat", tool: "shell" }), method: "hooks.test", params: { workspace, event: "PreToolUse", runtime_kind: "interactive_chat", tool: "shell" }, response: { workspace_root: workspace, event: "PreToolUse", matches: [], ignored_untrusted_project_paths: [], secret_values_included: false } },
    { call: () => client.hooksTrust({ workspace, workspace_trusted: true, target: "project_config" }), method: "hooks.trust", params: { workspace, workspace_trusted: true, target: "project_config" }, response: { target: "project_config", workspace_root: workspace, config_path: "/workspace/.alysis/hooks.json", trusted: true, changed: true, secret_values_included: false } },
    { call: () => client.hooksUntrust({ workspace, workspace_trusted: true, target: "project_config" }), method: "hooks.untrust", params: { workspace, workspace_trusted: true, target: "project_config" }, response: { target: "project_config", workspace_root: workspace, config_path: "/workspace/.alysis/hooks.json", trusted: false, changed: true, secret_values_included: false } },
    { call: () => client.hooksInit({ workspace, workspace_trusted: true, force: true }), method: "hooks.init", params: { workspace, workspace_trusted: true, force: true }, response: { workspace_root: workspace, config_path: "/workspace/.alysis/hooks.local.json", changed: true, gitignore_changed: false, secret_values_included: false } },
    { call: () => client.hooksEffective({ workspace, event: "PreToolUse", tool: "shell" }), method: "hooks.effective", params: { workspace, event: "PreToolUse", tool: "shell" }, response: { workspace_root: workspace, event: "PreToolUse", hooks: [], count: 0, secret_values_included: false } },
    { call: () => client.hooksEnable({ workspace, workspace_trusted: true, hook_id: "demo", layer: "local" }), method: "hooks.enable", params: { workspace, workspace_trusted: true, hook_id: "demo", layer: "local" }, response: { workspace_root: workspace, config_path: "/workspace/.alysis/hooks.local.json", hook_id: "demo", layer: "local", enabled: true, previous_enabled: false, changed: true, secret_values_included: false } },
    { call: () => client.hooksDisable({ workspace, workspace_trusted: true, hook_id: "demo", layer: "local" }), method: "hooks.disable", params: { workspace, workspace_trusted: true, hook_id: "demo", layer: "local" }, response: { workspace_root: workspace, config_path: "/workspace/.alysis/hooks.local.json", hook_id: "demo", layer: "local", enabled: false, previous_enabled: true, changed: true, secret_values_included: false } },
    { call: () => client.conventionsList({ workspace }), method: "conventions.list", params: { workspace }, response: { workspace_root: workspace, focus_path: workspace, documents: [], count: 0, secret_values_included: false } },
    { call: () => client.conventionsRender({ workspace, max_chars: 1000 }), method: "conventions.render", params: { workspace, max_chars: 1000 }, response: { workspace_root: workspace, focus_path: workspace, rendered: null, document_count: 0, truncated_or_limited: false, secret_values_included: false } },
    { call: () => client.extSearch({ query: "demo" }), method: "ext.search", params: { query: "demo" }, response: { extensions: [], count: 0, secret_values_included: false } },
    { call: () => client.extList({ workspace }), method: "ext.list", params: { workspace }, response: { workspace_root: workspace, extensions: [], count: 0, project_overrides: {}, secret_values_included: false } },
    { call: () => client.extInfo({ workspace, ext_id: "demo" }), method: "ext.info", params: { workspace, ext_id: "demo" }, response: { workspace_root: workspace, extension: { id: "demo" }, installed: null, installed_scopes: [], enabled_effective: false, project_override_state: "absent", workspace_trust: {}, secret_values_included: false } },
    { call: () => client.extInstall({ workspace, workspace_trusted: true, source: "demo", yes: true, trust_approval: { approved: true, plugin_id: "demo", commit: "a".repeat(40), manifest_sha256: "b".repeat(64), approval_fingerprint: "fingerprint" } }), method: "ext.install", params: { workspace, workspace_trusted: true, source: "demo", yes: true, trust_approval: { approved: true, plugin_id: "demo", commit: "a".repeat(40), manifest_sha256: "b".repeat(64), approval_fingerprint: "fingerprint" } }, response: { changed: true, result: {}, secret_values_included: false } },
    { call: () => client.extUninstall({ workspace, workspace_trusted: true, plugin_id: "demo", yes: true }), method: "ext.uninstall", params: { workspace, workspace_trusted: true, plugin_id: "demo", yes: true }, response: { changed: true, result: {}, secret_values_included: false } },
    { call: () => client.extEnable({ workspace, workspace_trusted: true, plugin_id: "demo", yes: true }), method: "ext.enable", params: { workspace, workspace_trusted: true, plugin_id: "demo", yes: true }, response: { changed: true, result: {}, secret_values_included: false } },
    { call: () => client.extDisable({ workspace, workspace_trusted: true, plugin_id: "demo", yes: true }), method: "ext.disable", params: { workspace, workspace_trusted: true, plugin_id: "demo", yes: true }, response: { changed: true, result: {}, secret_values_included: false } }
  ];

  for (let index = 0; index < calls.length; index += 1) {
    const pending = calls[index].call();
    await fake.waitForRequests(index + 1);
    assert.equal(fake.requests[index].method, calls[index].method);
    assert.deepEqual(fake.requests[index].params, calls[index].params);
    fake.respond(index, calls[index].response);
    await pending;
  }

  client.dispose();
});

test("AlysisBridgeClient sends and strictly parses MCP server lifecycle methods", async () => {
  const methods = [
    "mcp.server.status",
    "mcp.server.enable",
    "mcp.server.disable",
    "mcp.server.restart"
  ] as const;
  const fake = new FakeBridgeProcess({ initializePayload: healthPayloadWithMethods([...methods]) });
  const client = clientFor(fake);
  await client.startStdio(config());

  for (const method of methods) {
    assert.equal(client.supportsMethod(method), true);
  }

  const status = client.mcpServerStatus({ session_id: "session-1", server_id: "alpha" });
  await fake.waitForRequests(1);
  assert.equal(fake.requests[0].method, "mcp.server.status");
  assert.deepEqual(fake.requests[0].params, { session_id: "session-1", server_id: "alpha" });
  fake.respond(0, mcpServerLifecyclePayload());
  assert.deepEqual(await status, mcpServerLifecyclePayload());

  const enable = client.mcpServerEnable({
    session_id: "session-1",
    server_id: "alpha",
    workspace_trusted: true
  });
  await fake.waitForRequests(2);
  assert.equal(fake.requests[1].method, "mcp.server.enable");
  assert.deepEqual(fake.requests[1].params, {
    session_id: "session-1",
    server_id: "alpha",
    workspace_trusted: true
  });
  fake.respond(1, {
    ...mcpServerLifecyclePayload({ generation: 2 }),
    changed: true,
    action: "enable"
  });
  assert.deepEqual(await enable, {
    ...mcpServerLifecyclePayload({ generation: 2 }),
    changed: true,
    action: "enable"
  });

  const disable = client.mcpServerDisable({
    session_id: "session-1",
    server_id: "alpha",
    workspace_trusted: true
  });
  await fake.waitForRequests(3);
  assert.equal(fake.requests[2].method, "mcp.server.disable");
  fake.respond(2, {
    ...mcpServerLifecyclePayload({
      enabled: false,
      connection_state: "disabled",
      connected: false,
      generation: 2
    }),
    changed: true,
    action: "disable"
  });
  const disabled = await disable;
  assert.equal(disabled.connection_state, "disabled");
  assert.equal(disabled.action, "disable");

  const restart = client.mcpServerRestart({
    session_id: "session-1",
    server_id: "alpha",
    workspace_trusted: true
  });
  await fake.waitForRequests(4);
  assert.equal(fake.requests[3].method, "mcp.server.restart");
  fake.respond(3, {
    ...mcpServerLifecyclePayload({ generation: 3 }),
    changed: true,
    action: "restart"
  });
  assert.equal((await restart).generation, 3);

  client.dispose();
});

test("AlysisBridgeClient rejects malformed MCP server lifecycle responses", async () => {
  const fake = new FakeBridgeProcess({
    initializePayload: healthPayloadWithMethods(["mcp.server.status", "mcp.server.restart"])
  });
  const client = clientFor(fake);
  await client.startStdio(config());

  const leaked = client.mcpServerStatus({ session_id: "session-1", server_id: "alpha" });
  await fake.waitForRequests(1);
  fake.respond(0, mcpServerLifecyclePayload({ secret_values_included: true }));
  await assert.rejects(leaked, /secret_values_included is invalid/);

  const mismatched = client.mcpServerRestart({
    session_id: "session-1",
    server_id: "alpha",
    workspace_trusted: true
  });
  await fake.waitForRequests(2);
  fake.respond(1, {
    ...mcpServerLifecyclePayload(),
    changed: true,
    action: "disable"
  });
  await assert.rejects(mismatched, /action is invalid/);

  client.dispose();
});

test("AlysisBridgeClient requires live initialize before session creation", async () => {
  const fake = new FakeBridgeProcess({ initializePayload: { ok: false } });
  const client = clientFor(fake);

  await assert.rejects(
    client.startStdio(config()),
    (error) =>
      error instanceof Error &&
      /Unsupported Alysis Code IDE protocol version|health|ok|unhealthy/i.test(error.message)
  );
  assert.equal(client.isRunning(), false);
});

test("AlysisBridgeClient forwards explicit close-after-cancel intent", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.startStdio(config());

  const cancel = client.cancelSession("session-1", {
    reason: "new_session_requested",
    closeWhenIdle: true
  });
  await fake.waitForRequests(1);
  assert.deepEqual(fake.requests[0].params, {
    session_id: "session-1",
    reason: "new_session_requested",
    close_when_idle: true
  });
  fake.error(0, "cancel_not_supported", "Running jobs cannot be cancelled safely.");

  await assert.rejects(cancel, (error) => {
    assert.equal(error instanceof ProtocolClientError, true);
    assert.equal((error as ProtocolClientError).code, "cancel_not_supported");
    return true;
  });
  client.dispose();
});

test("AlysisBridgeClient warns honestly for active jobs during shutdown", async () => {
  const initializePayload = healthPayloadWithMethods(["session.cancel"]);
  const capabilities = initializePayload.capabilities as Record<string, unknown>;
  capabilities.features = {
    ...(capabilities.features as Record<string, unknown>),
    cancellation: { active_jobs: true }
  };
  const fake = new FakeBridgeProcess({
    initializePayload,
    sessionList: [
      {
        session_id: "session-1",
        workspace_root: "/workspace",
        mode: "review",
        closed: false,
        active_job: {
          job_id: "job-1",
          session_id: "session-1",
          status: "running"
        }
      }
    ],
    sessionCancelResult: { status: "cancellation_requested", job_id: "job-1" },
    jobStatusResults: [
      { job_id: "job-1", session_id: "session-1", status: "cancelled", state: "cancelled" }
    ],
    autoExitOnKill: true
  });
  const client = clientFor(fake);
  await client.startStdio(config());
  const warnings: string[] = [];
  client.on("stderr", (text) => warnings.push(text));

  await client.shutdown();

  assert.equal(fake.requests.some((request) => request.method === "session.cancel"), true);
  assert.equal(fake.requests.some((request) => request.method === "job.status"), true);
  assert.equal(warnings.some((text) => text.includes("cannot be cancelled by IDE protocol v1")), false);
  assert.equal(warnings.some((text) => text.includes("without reporting a cancelled status")), false);
});

test("AlysisBridgeClient warns from tracked active jobs when session listing fails during shutdown", async () => {
  const fake = new FakeBridgeProcess({
    sessionListError: true,
    autoExitOnKill: true
  });
  const client = clientFor(fake);
  await client.startStdio(config());
  const warnings: string[] = [];
  client.on("stderr", (text) => warnings.push(text));

  const started = client.sendChat("session-1", "hello");
  await fake.waitForRequests(1);
  fake.respond(0, { session_id: "session-1", job_id: "job-1", status: "started" });
  assert.equal((await started).job_id, "job-1");

  await client.shutdown();

  assert.equal(warnings.some((text) => text.includes("job-1")), true);
  assert.equal(warnings.some((text) => text.includes("cooperative cancellation could not be requested")), true);
  assert.equal(warnings.some((text) => text.includes("cannot be cancelled by IDE protocol v1")), false);
});

test("AlysisBridgeClient requests graceful bridge shutdown before process termination", async () => {
  const fake = new FakeBridgeProcess({
    initializePayload: healthPayloadWithMethods(["bridge.shutdown", "session.list"]),
    sessionList: [],
    autoExitOnKill: false
  });
  const client = clientFor(fake);
  await client.startStdio(config());

  const shutdown = client.shutdown();
  await fake.waitForRequests(2);
  assert.deepEqual(
    fake.requests.map((request) => request.method),
    ["session.list", "bridge.shutdown"]
  );
  fake.respond(1, { status: "shutting_down" });
  process.nextTick(() => fake.emit("exit", 0));

  await shutdown;
  assert.deepEqual(fake.killCalls, []);
});

test("AlysisBridgeClient starts and parses generic structured code reviews", async () => {
  const fake = new FakeBridgeProcess({
    initializePayload: healthPayloadWithMethods(["code.review.start", "code.review.result"])
  });
  const client = clientFor(fake);
  await client.startStdio(config());

  const start = client.codeReviewStart({
    session_id: "session-1",
    scope: "branch",
    base: "main",
    head: "HEAD",
    workspace_trusted: true
  });
  await fake.waitForRequests(1);
  assert.equal(fake.requests[0].method, "code.review.start");
  fake.respond(0, { session_id: "session-1", job_id: "job-review", status: "started", scope: "branch" });
  assert.equal((await start).scope, "branch");

  const result = client.codeReviewResult("job-review");
  await fake.waitForRequests(2);
  fake.respond(1, {
    session_id: "session-1",
    job_id: "job-review",
    status: "completed",
    scope: "branch",
    findings: [],
    summary: {
      verdict: "approve",
      overview: "Looks good.",
      finding_counts: { critical: 0 },
      changed_file_count: 1,
      reviewed_file_count: 1,
      omitted_file_count: 0,
      truncated: false,
      warnings: []
    },
    diff: {
      scope: "branch",
      changed_files: ["src/demo.ts"],
      included_files: ["src/demo.ts"],
      omitted_files: [],
      truncated: false,
      warnings: [],
      metadata: { base: "abc" },
      byte_count: 42
    }
  });

  assert.equal((await result).summary?.verdict, "approve");
});

test("AlysisBridgeClient round-trips durable structured tasks and questions", async () => {
  const fake = new FakeBridgeProcess({
    initializePayload: healthPayloadWithMethods([
      "session.tasks.get",
      "session.tasks.replace",
      "session.questions.create",
      "session.questions.get",
      "session.questions.list",
      "session.questions.answer",
      "session.questions.cancel"
    ])
  });
  const client = clientFor(fake);
  await client.startStdio(config());

  const getTasks = client.sessionTasksGet("session-1");
  await fake.waitForRequests(1);
  assert.equal(fake.requests[0].method, "session.tasks.get");
  assert.deepEqual(fake.requests[0].params, { session_id: "session-1" });
  fake.respond(0, { session_id: "session-1", revision: 0, tasks: [], updated_at: null });
  assert.equal((await getTasks).revision, 0);

  const tasks = [{ task_id: "T01", title: "Verify the bridge", status: "in_progress" as const }];
  const replaceTasks = client.sessionTasksReplace("session-1", 0, tasks, true);
  await fake.waitForRequests(2);
  assert.deepEqual(fake.requests[1].params, {
    session_id: "session-1",
    expected_revision: 0,
    tasks,
    workspace_trusted: true
  });
  fake.respond(1, { session_id: "session-1", revision: 1, tasks, updated: true });
  assert.equal((await replaceTasks).tasks?.[0].status, "in_progress");

  const questions = [{
    question_id: "target",
    prompt: "Which target?",
    options: [{ option_id: "tests", label: "Tests", description: "Run focused tests." }]
  }];
  const createQuestions = client.sessionQuestionsCreate({
    session_id: "session-1",
    idempotency_key: "question-key-1",
    questions,
    expires_in_seconds: 300,
    workspace_trusted: true
  });
  await fake.waitForRequests(3);
  assert.equal(fake.requests[2].method, "session.questions.create");
  fake.respond(2, structuredQuestionPayload({ questions, created: true }));
  assert.equal((await createQuestions).created, true);

  const getQuestions = client.sessionQuestionsGet("session-1", "questions-1");
  await fake.waitForRequests(4);
  assert.deepEqual(fake.requests[3].params, {
    session_id: "session-1",
    question_set_id: "questions-1"
  });
  fake.respond(3, structuredQuestionPayload({ questions }));
  assert.equal((await getQuestions).questions[0].question_id, "target");

  const listQuestions = client.sessionQuestionsList("session-1", {
    statuses: ["pending"],
    limit: 10
  });
  await fake.waitForRequests(5);
  assert.deepEqual(fake.requests[4].params, {
    session_id: "session-1",
    statuses: ["pending"],
    limit: 10
  });
  fake.respond(4, { question_sets: [structuredQuestionPayload({ questions })] });
  assert.equal((await listQuestions).length, 1);

  const answerQuestions = client.sessionQuestionsAnswer(
    "session-1",
    "questions-1",
    { target: "tests" },
    true
  );
  await fake.waitForRequests(6);
  assert.deepEqual(fake.requests[5].params, {
    session_id: "session-1",
    question_set_id: "questions-1",
    answers: { target: "tests" },
    workspace_trusted: true
  });
  fake.respond(5, structuredQuestionPayload({
    questions,
    status: "answered",
    answers: [{ question_id: "target", option_id: "tests" }]
  }));
  assert.equal((await answerQuestions).status, "answered");

  const cancelQuestions = client.sessionQuestionsCancel("session-1", "questions-1", true);
  await fake.waitForRequests(7);
  assert.equal(fake.requests[6].method, "session.questions.cancel");
  fake.respond(6, structuredQuestionPayload({ questions, status: "cancelled" }));
  assert.equal((await cancelQuestions).status, "cancelled");

  client.dispose();
});

test("AlysisBridgeClient supports event replay and artifact list/read", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.startStdio(config());

  const replay = client.getEvents("session-1", 3);
  await fake.waitForRequests(1);
  fake.respond(0, {
    session_id: "session-1",
    events: [eventEnvelope({ sequence: 4, type: "status_update", payload: { mode: "readonly" } })],
    truncated: true,
    lowest_retained_sequence: 4,
    highest_retained_sequence: 4,
    max_events: 500
  });
  const replayResult = await replay;
  assert.equal(replayResult.truncated, true);
  assert.equal(replayResult.events[0].sequence, 4);

  const artifacts = client.artifactList("session-1");
  await fake.waitForRequests(2);
  fake.respond(1, {
    session_id: "session-1",
    artifacts: [{ artifact_id: "session:log.txt", root: "session", path: "log.txt", size_bytes: 5 }],
    truncated: false,
    max_items: 500,
    max_depth: 8
  });
  assert.equal((await artifacts).artifacts[0].artifact_id, "session:log.txt");

  const content = client.artifactRead("session-1", "session:log.txt");
  await fake.waitForRequests(3);
  fake.respond(2, {
    session_id: "session-1",
    artifact_id: "session:log.txt",
    path: "log.txt",
    size_bytes: 5,
    truncated: false,
    max_bytes: 65536,
    encoding: "utf-8-replace",
    content: "hello"
  });
  assert.equal((await content).content, "hello");
  client.dispose();
});

test("AlysisBridgeClient exposes the complete managed-browser protocol with typed results", async () => {
  const browserMethods = [
    "browser.start",
    "browser.navigate",
    "browser.snapshot",
    "browser.screenshot",
    "browser.artifact.read",
    "browser.diagnostics",
    "browser.click",
    "browser.type",
    "browser.status",
    "browser.list",
    "browser.close"
  ];
  const fake = new FakeBridgeProcess({
    initializePayload: healthPayloadWithMethods(browserMethods)
  });
  const client = clientFor(fake);
  await client.startStdio(config());
  assert.equal(client.supportsMethod("browser.artifact.read"), true);

  const ideSessionId = "session-1";
  const browserSessionId = "browser-session-12345678";
  const startParams = {
    session_id: ideSessionId,
    workspace_trusted: true,
    network_scope: "public_loopback" as const,
    yes: true,
    confirm: true
  };
  const started = client.browserStart(startParams);
  await fake.waitForRequests(1);
  assert.equal(fake.requests[0].method, "browser.start");
  assert.deepEqual(fake.requests[0].params, startParams);
  fake.respond(0, browserStatusPayload({
    browser_session_id: browserSessionId,
    network_scope: "public_loopback",
    allow_local_destinations: true
  }));
  assert.equal((await started).network_scope, "public_loopback");

  const navigateParams = {
    session_id: ideSessionId,
    browser_session_id: browserSessionId,
    workspace_trusted: true,
    url: "https://example.com/docs",
    timeout_seconds: 12
  };
  const navigated = client.browserNavigate(navigateParams);
  await fake.waitForRequests(2);
  assert.equal(fake.requests[1].method, "browser.navigate");
  assert.deepEqual(fake.requests[1].params, navigateParams);
  fake.respond(1, {
    session_id: ideSessionId,
    browser_session_id: browserSessionId,
    url: navigateParams.url,
    result: { data: { frame_id: "frame-1" }, truncated: false, size_bytes: 22 }
  });
  assert.deepEqual((await navigated).result.data, { frame_id: "frame-1" });

  const snapshot = client.browserSnapshot({
    session_id: ideSessionId,
    browser_session_id: browserSessionId,
    kind: "text",
    timeout_seconds: 5
  });
  await fake.waitForRequests(3);
  fake.respond(2, {
    session_id: ideSessionId,
    browser_session_id: browserSessionId,
    kind: "text",
    text: "Documentation",
    truncated: false,
    size_bytes: 13
  });
  const snapshotResult = await snapshot;
  assert.equal(snapshotResult.kind, "text");
  assert.equal(snapshotResult.kind === "text" ? snapshotResult.text : "", "Documentation");

  const screenshot = client.browserScreenshot({
    session_id: ideSessionId,
    browser_session_id: browserSessionId,
    full_page: true,
    timeout_seconds: 8
  });
  await fake.waitForRequests(4);
  fake.respond(3, {
    session_id: ideSessionId,
    browser_session_id: browserSessionId,
    artifact_id: `browser:${browserSessionId}:screenshot-0001-deadbeef.png`,
    media_type: "image/png",
    size_bytes: 128,
    sha256: "a".repeat(64)
  });
  const screenshotResult = await screenshot;
  assert.equal(screenshotResult.media_type, "image/png");

  const artifact = client.browserArtifactRead({
    session_id: ideSessionId,
    browser_session_id: browserSessionId,
    artifact_id: screenshotResult.artifact_id,
    offset: 0,
    max_bytes: 64
  });
  await fake.waitForRequests(5);
  assert.equal(fake.requests[4].method, "browser.artifact.read");
  fake.respond(4, {
    session_id: ideSessionId,
    browser_session_id: browserSessionId,
    artifact_id: screenshotResult.artifact_id,
    media_type: "image/png",
    encoding: "base64",
    content: "iVBORw0KGgo=",
    offset: 0,
    next_offset: 8,
    size_bytes: 128,
    truncated: true
  });
  assert.equal((await artifact).next_offset, 8);

  const diagnostics = client.browserDiagnostics({
    session_id: ideSessionId,
    browser_session_id: browserSessionId,
    max_events: 10,
    timeout_seconds: 2
  });
  await fake.waitForRequests(6);
  fake.respond(5, {
    session_id: ideSessionId,
    browser_session_id: browserSessionId,
    events: [{
      category: "console",
      method: "Runtime.consoleAPICalled",
      params: { data: { type: "warning" }, truncated: false, size_bytes: 18 }
    }],
    truncated: false,
    max_events: 10
  });
  assert.equal((await diagnostics).events[0]?.category, "console");

  const clicked = client.browserClick({
    session_id: ideSessionId,
    browser_session_id: browserSessionId,
    workspace_trusted: true,
    selector: "button[type=submit]",
    timeout_seconds: 4
  });
  await fake.waitForRequests(7);
  fake.respond(6, {
    session_id: ideSessionId,
    browser_session_id: browserSessionId,
    clicked: true
  });
  assert.equal((await clicked).clicked, true);

  const typed = client.browserType({
    session_id: ideSessionId,
    browser_session_id: browserSessionId,
    workspace_trusted: true,
    selector: "input[name=query]",
    text: "Alysis Code",
    replace: true,
    timeout_seconds: 4
  });
  await fake.waitForRequests(8);
  fake.respond(7, {
    session_id: ideSessionId,
    browser_session_id: browserSessionId,
    typed: true,
    character_count: 9
  });
  assert.equal((await typed).character_count, 9);

  const status = client.browserStatus({
    session_id: ideSessionId,
    browser_session_id: browserSessionId
  });
  await fake.waitForRequests(9);
  fake.respond(8, browserStatusPayload({ browser_session_id: browserSessionId, artifact_count: 1 }));
  assert.equal((await status).artifact_count, 1);

  const listed = client.browserList(ideSessionId);
  await fake.waitForRequests(10);
  fake.respond(9, {
    session_id: ideSessionId,
    browsers: [browserStatusPayload({ browser_session_id: browserSessionId, artifact_count: 1 })],
    count: 1
  });
  assert.equal((await listed).browsers[0]?.browser_session_id, browserSessionId);

  const closeParams = {
    session_id: ideSessionId,
    browser_session_id: browserSessionId,
    workspace_trusted: true,
    delete_artifacts: true,
    yes: true
  };
  const closed = client.browserClose(closeParams);
  await fake.waitForRequests(11);
  assert.equal(fake.requests[10].method, "browser.close");
  assert.deepEqual(fake.requests[10].params, closeParams);
  fake.respond(10, {
    session_id: ideSessionId,
    browser_session_id: browserSessionId,
    status: "closed"
  });
  assert.equal((await closed).status, "closed");
  client.dispose();
});

test("AlysisBridgeClient rejects malformed managed-browser payloads", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.startStdio(config());
  const screenshot = client.browserScreenshot({
    session_id: "session-1",
    browser_session_id: "browser-session-12345678"
  });
  await fake.waitForRequests(1);
  fake.respond(0, {
    session_id: "session-1",
    browser_session_id: "browser-session-12345678",
    artifact_id: "artifact-1",
    media_type: "text/html",
    size_bytes: 12,
    sha256: "a".repeat(64)
  });
  await assert.rejects(screenshot, /media_type is invalid/);
  client.dispose();
});

test("AlysisBridgeClient parses Forge plan/status and diff protocol responses", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.startStdio(config());

  const plan = client.forgePlan({
    workspace: "/workspace",
    instruction: "Prepare Forge protocol foundation.",
    mode: "readonly"
  });
  await fake.waitForRequests(1);
  assert.equal(fake.requests[0].method, "forge.plan");
  assert.equal(fake.requests[0].params.instruction, "Prepare Forge protocol foundation.");
  fake.respond(0, forgePlanPayload());
  const planResult = await plan;
  assert.equal(planResult.plan_id, "plan-1");
  assert.equal(planResult.tasks[0].task_id, "T01");
  assert.deepEqual(planResult.tasks[0].file_scope.write_scope, ["src/ide"]);

  const status = client.forgeStatus("session-1", "plan-1");
  await fake.waitForRequests(2);
  assert.equal(fake.requests[1].method, "forge.status");
  fake.respond(1, { ...forgePlanPayload(), diff_count: 1 });
  assert.equal((await status).diff_count, 1);

  const diffs = client.diffList("session-1", "plan-1");
  await fake.waitForRequests(3);
  assert.equal(fake.requests[2].method, "diff.list");
  fake.respond(2, {
    diffs: [
      {
        diff_id: "diff-1",
        session_id: "session-1",
        plan_id: "plan-1",
        job_id: null,
        file_path: "README.md",
        status: "available",
        old_label: "before",
        new_label: "after",
        size_bytes: 42
      }
    ]
  });
  assert.equal((await diffs).diffs[0].file_path, "README.md");

  const diff = client.diffGet("diff-1", { sessionId: "session-1", planId: "plan-1", maxBytes: 1024 });
  await fake.waitForRequests(4);
  assert.equal(fake.requests[3].method, "diff.get");
  assert.deepEqual(fake.requests[3].params, {
    diff_id: "diff-1",
    session_id: "session-1",
    plan_id: "plan-1",
    max_bytes: 1024
  });
  fake.respond(3, {
    diff_id: "diff-1",
    session_id: "session-1",
    plan_id: "plan-1",
    job_id: null,
    file_path: "README.md",
    old_text: null,
    new_text: null,
    old_artifact_id: null,
    new_artifact_id: null,
    unified_diff: "--- a/README.md\n+++ b/README.md\n",
    truncated: false,
    size_bytes: 42,
    max_bytes: 1024,
    redaction: "protocol_preview"
  });
  assert.equal((await diff).unified_diff.includes("README.md"), true);
  client.dispose();
});

test("AlysisBridgeClient parses async Forge Plan start and result responses", async () => {
  const fake = new FakeBridgeProcess({
    initializePayload: healthPayloadWithMethods(["forge.plan.start", "forge.plan.result"])
  });
  const client = clientFor(fake);
  await client.startStdio(config());
  assert.equal(client.supportsMethod("forge.plan.start"), true);

  const started = client.forgePlanStart({
    workspace: "/workspace",
    instruction: "Plan asynchronously.",
    mode: "readonly",
    idempotency_key: "forge-request-1"
  });
  await fake.waitForRequests(1);
  assert.equal(fake.requests[0].method, "forge.plan.start");
  assert.equal(fake.requests[0].params.idempotency_key, "forge-request-1");
  fake.respond(0, {
    session_id: "session-1",
    job_id: "job-plan-1",
    status: "started",
    durably_accepted: true,
    duplicate: false
  });
  assert.deepEqual(await started, {
    session_id: "session-1",
    job_id: "job-plan-1",
    status: "started",
    durably_accepted: true,
    duplicate: false
  });

  const result = client.forgePlanResult("job-plan-1");
  await fake.waitForRequests(2);
  assert.equal(fake.requests[1].method, "forge.plan.result");
  assert.deepEqual(fake.requests[1].params, { job_id: "job-plan-1" });
  fake.respond(1, { ...forgePlanPayload(), job_id: "job-plan-1" });
  assert.equal((await result).job_id, "job-plan-1");
  client.dispose();
});

test("AlysisBridgeClient public RPC methods are safe when detached", async () => {
  const fake = new FakeBridgeProcess({
    initializePayload: healthPayloadWithMethods(["forge.plan.start", "forge.plan.result"])
  });
  const client = clientFor(fake);
  await client.startStdio(config());

  const sendChat = client.sendChat;
  const startRun = client.startRun;
  const forgePlanStart = client.forgePlanStart;
  const forgePlanResult = client.forgePlanResult;
  const jobStatus = client.jobStatus;

  const chat = sendChat("session-1", "hello");
  await fake.waitForRequests(1);
  assert.equal(fake.requests[0].method, "chat.send");
  fake.respond(0, { session_id: "session-1", job_id: "job-chat-1", status: "started" });
  assert.equal((await chat).job_id, "job-chat-1");
  assert.equal(client.canRestartSafely(), false);
  const chatStatus = jobStatus("job-chat-1");
  await fake.waitForRequests(2);
  fake.respond(1, { job_id: "job-chat-1", session_id: "session-1", status: "completed" });
  assert.equal((await chatStatus).status, "completed");
  assert.equal(client.canRestartSafely(), true);

  const run = startRun({ workspace: "/workspace", instruction: "run", mode: "readonly" });
  await fake.waitForRequests(3);
  assert.equal(fake.requests[2].method, "run.start");
  fake.respond(2, { session_id: "session-2", job_id: "job-run-1", status: "started" });
  assert.equal((await run).job_id, "job-run-1");
  assert.equal(client.canRestartSafely(), false);
  const runStatus = jobStatus("job-run-1");
  await fake.waitForRequests(4);
  fake.respond(3, { job_id: "job-run-1", session_id: "session-2", status: "completed" });
  assert.equal((await runStatus).status, "completed");
  assert.equal(client.canRestartSafely(), true);

  const started = forgePlanStart({
    workspace: "/workspace",
    instruction: "Plan asynchronously.",
    mode: "readonly"
  });
  await fake.waitForRequests(5);
  assert.equal(fake.requests[4].method, "forge.plan.start");
  fake.respond(4, { session_id: "session-3", job_id: "job-plan-1", status: "started" });
  assert.equal((await started).job_id, "job-plan-1");
  assert.equal(client.canRestartSafely(), false);

  const result = forgePlanResult("job-plan-1");
  await fake.waitForRequests(6);
  assert.equal(fake.requests[5].method, "forge.plan.result");
  fake.respond(5, { ...forgePlanPayload(), job_id: "job-plan-1" });
  assert.equal((await result).job_id, "job-plan-1");
  const planStatus = jobStatus("job-plan-1");
  await fake.waitForRequests(7);
  fake.respond(6, { job_id: "job-plan-1", session_id: "session-3", status: "completed" });
  assert.equal((await planStatus).status, "completed");
  assert.equal(client.canRestartSafely(), true);

  // SW6: the swarm client methods must also survive being detached (the
  // cockpit calls them through the auto-bound BridgeClientLike receiver).
  const forgeSwarmStart = client.forgeSwarmStart;
  const forgeSwarmResult = client.forgeSwarmResult;
  const forgeSwarmReview = client.forgeSwarmReview;
  const forgeSwarmApply = client.forgeSwarmApply;
  const forgeSwarmDiscard = client.forgeSwarmDiscard;
  const forgeSwarmCancel = client.forgeSwarmCancel;

  const swarmStart = forgeSwarmStart({
    session_id: "session-3",
    plan_id: "plan-1",
    workspace_trusted: true,
    parallel: 3,
    approval_scope_grants: []
  });
  await fake.waitForRequests(8);
  assert.equal(fake.requests[7].method, "forge.swarm.start");
  fake.respond(7, { session_id: "session-3", plan_id: "plan-1", job_id: "job-swarm-1", status: "started", parallel: 3 });
  assert.equal((await swarmStart).job_id, "job-swarm-1");

  const swarmProgress = forgeSwarmResult("job-swarm-1");
  await fake.waitForRequests(9);
  assert.equal(fake.requests[8].method, "forge.swarm.result");
  fake.respond(8, { session_id: "session-3", job_id: "job-swarm-1", plan_id: "plan-1", status: "running", state: "running", complete: false });
  const progress = await swarmProgress;
  assert.equal("complete" in progress ? progress.complete : true, false);

  const swarmReview = forgeSwarmReview("session-3", "plan-1");
  await fake.waitForRequests(10);
  assert.equal(fake.requests[9].method, "forge.swarm.review");
  fake.respond(9, {
    session_id: "session-3",
    plan_id: "plan-1",
    items: [],
    state_counts: {},
    pending_review_task_ids: [],
    working_tree_untouched_until_apply: true
  });
  assert.deepEqual((await swarmReview).pending_review_task_ids, []);

  const swarmApply = forgeSwarmApply("session-3", "plan-1", ["T01"]);
  await fake.waitForRequests(11);
  assert.equal(fake.requests[10].method, "forge.swarm.apply");
  fake.respond(10, { session_id: "session-3", plan_id: "plan-1", applied: [{ task_id: "T01", applied: true }], working_tree_committed: false });
  assert.equal((await swarmApply).working_tree_committed, false);

  const swarmDiscard = forgeSwarmDiscard("session-3", "plan-1", ["T02"]);
  await fake.waitForRequests(12);
  assert.equal(fake.requests[11].method, "forge.swarm.discard");
  fake.respond(11, { session_id: "session-3", plan_id: "plan-1", discarded: [{ task_id: "T02", discarded: true }] });
  assert.equal((await swarmDiscard).discarded.length, 1);

  const swarmCancel = forgeSwarmCancel("session-3", "job-swarm-1", "cancelled_by_user");
  await fake.waitForRequests(13);
  assert.equal(fake.requests[12].method, "forge.swarm.cancel");
  fake.respond(12, { session_id: "session-3", plan_id: "plan-1", job_id: "job-swarm-1", status: "cancellation_requested", state: "cancellation_requested" });
  await swarmCancel;

  client.dispose();
});

test("AlysisBridgeClient parses durable Forge list/open responses", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.startStdio(config());

  const list = client.forgeList({ workspace: "/workspace", max_items: 25 });
  await fake.waitForRequests(1);
  assert.equal(fake.requests[0].method, "forge.list");
  assert.deepEqual(fake.requests[0].params, { workspace: "/workspace", max_items: 25 });
  fake.respond(0, {
    workspace_root: "/workspace",
    plans: [
      {
        plan_id: "plan-1",
        session_id: null,
        workspace_root: "/workspace",
        status: "planned",
        source: "persisted",
        project_goal: "Goal",
        summary: "Summary",
        task_count: 1,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-02T00:00:00Z",
        plan_artifact_id: "forge_plan_1:plan/plan.json",
        plan_markdown_artifact_id: "forge_plan_1:plan/PLAN.md"
      }
    ],
    truncated: false,
    max_items: 25
  });
  const listResult = await list;
  assert.equal(listResult.plans[0].source, "persisted");
  assert.equal(listResult.plans[0].task_count, 1);

  const open = client.forgeOpen({ workspace: "/workspace", plan_id: "plan-1" });
  await fake.waitForRequests(2);
  assert.equal(fake.requests[1].method, "forge.open");
  fake.respond(1, { ...forgePlanPayload(), source: "loaded_persisted", created_session: true });
  const opened = await open;
  assert.equal(opened.source, "loaded_persisted");
  assert.equal(opened.created_session, true);
  client.dispose();
});

test("AlysisBridgeClient resumes and lists durable Forge swarm jobs across restarts", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.startStdio(config());

  const resume = client.forgeSwarmResume({
    session_id: "session-3",
    plan_id: "plan-1",
    job_id: "job-swarm-1",
    workspace_trusted: true,
    approval_scope_grants: [{ kind: "command", scope: { executable: "npm" } }],
    parallel: 3,
    expected_revision: 7
  });
  await fake.waitForRequests(1);
  assert.equal(fake.requests[0].method, "forge.swarm.resume");
  assert.deepEqual(fake.requests[0].params, {
    session_id: "session-3",
    plan_id: "plan-1",
    job_id: "job-swarm-1",
    workspace_trusted: true,
    approval_scope_grants: [{ kind: "command", scope: { executable: "npm" } }],
    parallel: 3,
    expected_revision: 7
  });
  fake.respond(0, {
    session_id: "session-3",
    plan_id: "plan-1",
    status: "resumed",
    ...durableSwarmStatusPayload({ state: "queued", revision: 8, resume_count: 1 })
  });
  const resumed = await resume;
  assert.equal(resumed.job_id, "job-swarm-1");
  assert.equal(resumed.status, "resumed");
  assert.equal(resumed.revision, 8);
  assert.equal(resumed.resume_count, 1);
  assert.equal(resumed.usage.total_tokens, 15);

  const list = client.forgeSwarmList({ session_id: "session-3", limit: 25 });
  await fake.waitForRequests(2);
  assert.equal(fake.requests[1].method, "forge.swarm.list");
  assert.deepEqual(fake.requests[1].params, { session_id: "session-3", limit: 25 });
  fake.respond(1, {
    session_id: "session-3",
    jobs: [durableSwarmStatusPayload({ state: "interrupted", resumable: true })],
    count: 1
  });
  const listed = await list;
  assert.equal(listed.count, 1);
  assert.equal(listed.jobs[0]?.state, "interrupted");
  assert.equal(listed.jobs[0]?.resumable, true);

  const recoveredResult = client.forgeSwarmResult("job-swarm-1", "session-3");
  await fake.waitForRequests(3);
  assert.equal(fake.requests[2].method, "forge.swarm.result");
  assert.deepEqual(fake.requests[2].params, {
    job_id: "job-swarm-1",
    session_id: "session-3"
  });
  fake.respond(2, {
    job_id: "job-swarm-1",
    status: "completed",
    state: "succeeded",
    complete: true
  });
  assert.equal((await recoveredResult).job_id, "job-swarm-1");
  assert.equal(client.canRestartSafely(), true);

  client.dispose();
});

test("AlysisBridgeClient parses Forge show/review/assets backend methods", async () => {
  const fake = new FakeBridgeProcess({
    initializePayload: healthPayloadWithMethods([
      "forge.show",
      "forge.review",
      "forge.assets.add",
      "forge.assets.list"
    ])
  });
  const client = clientFor(fake);
  await client.startStdio(config());

  const show = client.forgeShow({ session_id: "session-1", plan_id: "plan-1" });
  await fake.waitForRequests(1);
  assert.equal(fake.requests[0].method, "forge.show");
  fake.respond(0, {
    ...forgePlanPayload(),
    assets: [forgeAssetEntryPayload()],
    legacy_assets: [{ stored_path: ".alysis/runs/plan-1/plan/assets/legacy.txt" }],
    artifact_count: 2
  });
  const shown = await show;
  assert.equal(shown.assets[0].record.id, "asset_1");
  assert.equal(shown.legacy_assets.length, 1);

  const add = client.forgeAssetsAdd({
    session_id: "session-1",
    plan_id: "plan-1",
    workspace_trusted: true,
    source_path: "docs/spec.md",
    title: "Spec",
    wait: false
  });
  await fake.waitForRequests(2);
  assert.equal(fake.requests[1].method, "forge.assets.add");
  assert.equal(fake.requests[1].params.workspace_trusted, true);
  fake.respond(1, {
    session_id: "session-1",
    plan_id: "plan-1",
    asset: forgeAssetDetailPayload(),
    bound_task_ids: ["T01"],
    status: "added"
  });
  const added = await add;
  assert.equal((added.asset as any).record.id, "asset_1");
  assert.deepEqual(added.bound_task_ids, ["T01"]);

  const review = client.forgeReview({
    session_id: "session-1",
    plan_id: "plan-1",
    task_id: "T01",
    workspace_trusted: true
  });
  await fake.waitForRequests(3);
  assert.equal(fake.requests[2].method, "forge.review");
  fake.respond(2, {
    session_id: "session-1",
    plan_id: "plan-1",
    task_id: "T01",
    approved: false,
    confidence: "medium",
    summary: "Needs changes.",
    blocking_issues_count: 1,
    non_blocking_issues_count: 0,
    review_json: { approved: false },
    review_markdown: "# Review\n",
    json_artifact_id: "forge_plan_1:execution/reviews/T01.json",
    markdown_artifact_id: "forge_plan_1:execution/reviews/T01.md",
    requires_human_approval: true,
    action: { kind: "review_needed" }
  });
  const reviewed = await review;
  assert.equal(reviewed.requires_human_approval, true);
  assert.equal(reviewed.action?.kind, "review_needed");
  client.dispose();
});

test("AlysisBridgeClient sends typed Forge plan editing methods", async () => {
  const fake = new FakeBridgeProcess({
    initializePayload: healthPayloadWithMethods([
      "forge.plan.getState",
      "forge.plan.setAssistant",
      "forge.plan.setGoal",
      "forge.plan.updateTask",
      "forge.plan.validate",
      "forge.plan.regenerate"
    ])
  });
  const client = clientFor(fake);
  await client.startStdio(config());

  const state = client.forgePlanGetState({ session_id: "session-1", plan_id: "plan-1" });
  await fake.waitForRequests(1);
  assert.equal(fake.requests[0].method, "forge.plan.getState");
  fake.respond(0, forgePlanStatePayload({ changed: false }));
  assert.equal((await state).assistant.instruction, "Use scoped edits.");

  const assistant = client.forgePlanSetAssistant({
    session_id: "session-1",
    plan_id: "plan-1",
    workspace_trusted: true,
    instruction: "Use review mode.",
    expected_revision: 1
  });
  await fake.waitForRequests(2);
  assert.equal(fake.requests[1].method, "forge.plan.setAssistant");
  assert.equal(fake.requests[1].params.workspace_trusted, true);
  fake.respond(1, forgePlanStatePayload({ changed: true, ide_revision: 2 }));
  assert.equal((await assistant).ide_revision, 2);

  const goal = client.forgePlanSetGoal({
    session_id: "session-1",
    plan_id: "plan-1",
    workspace_trusted: true,
    goal: "Ship typed Forge parity.",
    expected_revision: 2
  });
  await fake.waitForRequests(3);
  assert.equal(fake.requests[2].method, "forge.plan.setGoal");
  assert.equal(fake.requests[2].params.goal, "Ship typed Forge parity.");
  fake.respond(2, forgePlanStatePayload({ changed: true, goal: "Ship typed Forge parity.", ide_revision: 3 }));
  assert.equal((await goal).goal, "Ship typed Forge parity.");

  const task = client.forgePlanUpdateTask({
    session_id: "session-1",
    plan_id: "plan-1",
    task_id: "T01",
    status: "blocked",
    workspace_trusted: true,
    expected_revision: 3
  });
  await fake.waitForRequests(4);
  assert.equal(fake.requests[3].method, "forge.plan.updateTask");
  assert.equal(fake.requests[3].params.task_id, "T01");
  fake.respond(3, forgePlanStatePayload({ changed: true, task: forgeTaskPayload({ status: "blocked" }) }));
  assert.equal((await task).task?.status, "blocked");

  const validate = client.forgePlanValidate({ session_id: "session-1", plan_id: "plan-1" });
  await fake.waitForRequests(5);
  assert.equal(fake.requests[4].method, "forge.plan.validate");
  fake.respond(4, { ok: true, missing: [] });
  assert.equal((await validate).ok, true);

  const regenerate = client.forgePlanRegenerate({
    session_id: "session-1",
    plan_id: "plan-1",
    workspace_trusted: true,
    expected_revision: 3,
    instruction: "Refresh the plan.",
    focus: "demo.py"
  });
  await fake.waitForRequests(6);
  assert.equal(fake.requests[5].method, "forge.plan.regenerate");
  assert.equal(fake.requests[5].params.workspace_trusted, true);
  fake.respond(5, forgePlanStatePayload({
    changed: true,
    old_revision: 3,
    new_revision: 4,
    ide_revision: 4,
    redacted: true,
    secret_values_included: false
  }));
  const regenerated = await regenerate;
  assert.equal(regenerated.old_revision, 3);
  assert.equal(regenerated.new_revision, 4);
  assert.equal(regenerated.secret_values_included, false);

  client.dispose();
});

test("AlysisBridgeClient separates forge.cancel method presence from feature support", async () => {
  const initializePayload = healthPayloadWithMethods(["forge.cancel"]);
  const capabilities = initializePayload.capabilities as Record<string, unknown>;
  capabilities.features = {
    forge: {
      cancel: { supported: false }
    }
  };
  const fake = new FakeBridgeProcess({ initializePayload });
  const client = clientFor(fake);
  await client.startStdio(config());

  assert.equal(client.supportsMethod("forge.cancel"), true);
  assert.equal(client.supportsFeature(["forge", "cancel", "supported"]), false);
  client.dispose();
});

test("AlysisBridgeClient parses Forge Execute Preview responses", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.startStdio(config());

  const preview = client.forgeExecutePreview({
    session_id: "session-1",
    plan_id: "plan-1",
    task_ids: ["T01"],
    mode: "readonly",
    workspace_trusted: false,
    sandbox_profile: "default"
  });
  await fake.waitForRequests(1);
  assert.equal(fake.requests[0].method, "forge.executePreview");
  assert.deepEqual(fake.requests[0].params, {
    session_id: "session-1",
    plan_id: "plan-1",
    task_ids: ["T01"],
    mode: "readonly",
    workspace_trusted: false,
    sandbox_profile: "default"
  });
  fake.respond(0, forgeExecutePreviewPayload());
  const result = await preview;
  assert.equal(result.selected_task_ids[0], "T01");
  assert.equal(result.execution_mode_requested, "readonly");
  assert.equal(result.workspace_trust_required, false);
  assert.equal(result.sandbox_profile.supported, true);
  assert.equal(result.sandbox_profile.available, true);
  assert.equal(result.required_approvals[0].allow_for_session_scope?.type, "exact_file_set");
  assert.equal(result.real_execution_supported, false);
  assert.equal(result.max_steps, 5);
  assert.equal(result.no_log, true);
  assert.equal(result.subagents_supported, false);
  assert.equal(result.subagents_enabled, false);
  assert.equal(result.subagents_policy, "disabled_for_ide_forge_execute_v1");
  client.dispose();
});

test("AlysisBridgeClient surfaces unsupported Forge and diff errors clearly", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.startStdio(config());

  const execute = client.forgeExecute({
    session_id: "session-1",
    plan_id: "plan-1",
    mode: "review"
  });
  await fake.waitForRequests(1);
  assert.equal(fake.requests[0].method, "forge.execute");
  fake.error(0, "forge_execute_unsupported", "forge.execute is fail-closed.");
  await assert.rejects(execute, (error) => {
    assert.equal(error instanceof ProtocolClientError, true);
    assert.equal((error as ProtocolClientError).code, "forge_execute_unsupported");
    return true;
  });

  const diff = client.diffGet("diff-missing", { sessionId: "session-1", planId: "plan-1" });
  await fake.waitForRequests(2);
  fake.error(1, "diff_not_found", "Diff was not found.");
  await assert.rejects(diff, (error) => {
    assert.equal(error instanceof ProtocolClientError, true);
    assert.equal((error as ProtocolClientError).code, "diff_not_found");
    return true;
  });
  client.dispose();
});

test("AlysisBridgeClient redacts secrets from stderr events", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.startStdio(config());

  const stderr = once(client, "stderr") as Promise<[string]>;
  fake.stderr.write("Authorization: Bearer abcdefgh1234567890\n");
  const [line] = await stderr;

  assert.equal(line.includes("abcdefgh1234567890"), false);
  assert.equal(line, "Authorization: <redacted>");
  client.dispose();
});

test("the bridge environment carries proxy, CA and parent-PID plumbing even when credentials are stripped", async () => {
  const fake = new FakeBridgeProcess();
  let captured: NodeJS.ProcessEnv | undefined;
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    (_command, _args, options) => {
      captured = options.env;
      return fake;
    }
  );

  await client.startStdio(
    {
      ...config(),
      hostNetwork: {
        proxySupport: "override",
        proxy: "http://proxy.corp.example:3128",
        noProxy: ["localhost", ".corp.example"],
        extraCaCerts: "/etc/pki/corp-ca.pem"
      }
    },
    { apiKey: "bridge-secret", stripApiKey: true }
  );

  assert.ok(captured);
  assert.equal(captured.HTTP_PROXY, "http://proxy.corp.example:3128");
  assert.equal(captured.HTTPS_PROXY, "http://proxy.corp.example:3128");
  assert.equal(captured.NO_PROXY, "localhost,.corp.example");
  assert.equal(captured.ALYSIS_EXTRA_CA_CERTS, "/etc/pki/corp-ca.pem");
  assert.equal(captured.NODE_EXTRA_CA_CERTS, "/etc/pki/corp-ca.pem");
  assert.equal(captured.ALYSIS_PARENT_PID, String(process.pid));
  assert.equal(captured.ALYSIS_API_KEY, undefined, "stripApiKey must still strip credentials");
  client.dispose();
});

test("AlysisBridgeClient passes SecretStorage API key through the bridge environment only", async () => {
  const fake = new FakeBridgeProcess();
  let capturedApiKey: string | undefined;
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    (_command, _args, options) => {
      capturedApiKey = options.env.ALYSIS_API_KEY;
      return fake;
    }
  );

  await client.startStdio(config(), { apiKey: "bridge-secret" });

  assert.equal(capturedApiKey, "bridge-secret");
  client.dispose();
});

test("custom provider keys without vendor aliases reach only their profile and refresh on replacement", async () => {
  const captured: NodeJS.ProcessEnv[] = [];
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    (_command, _args, options) => {
      captured.push(options.env);
      return new FakeBridgeProcess();
    }
  );
  try {
    const options = { providerCredentials: [{ profile: "custom", envVar: "", value: "custom-key" }] };
    await client.ensureStartedForProfile(config(), options, { credentialsRequired: true });
    assert.equal(captured[0].ALYSIS_IDE_PROFILE_637573746F6D_API_KEY, "custom-key");
    assert.equal(captured[0][""], undefined);
    assert.equal(Object.values(captured[0]).filter(value => value === "custom-key").length, 1);
    options.providerCredentials[0].value = "replacement-custom-key";
    const updated = await client.ensureStartedForProfile(config(), options, { credentialsRequired: true });
    assert.equal(updated.restarted, true);
    assert.equal(captured[1].ALYSIS_IDE_PROFILE_637573746F6D_API_KEY, "replacement-custom-key");
  } finally {
    client.dispose();
  }
});

test("custom provider keys stay out of credential-stripped probes", async () => {
  let captured: NodeJS.ProcessEnv = {};
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    (_command, _args, options) => { captured = options.env; return new FakeBridgeProcess(); }
  );
  try {
    await client.startStdio(config(), {
      stripApiKey: true,
      providerCredentials: [{ profile: "custom", envVar: "", value: "custom-key" }]
    });
    assert.equal(captured.ALYSIS_IDE_PROFILE_637573746F6D_API_KEY, undefined);
    assert.equal(Object.values(captured).includes("custom-key"), false);
  } finally {
    client.dispose();
  }
});

test("provider-scoped keys are exported under each profile's declared env var", async () => {
  const fake = new FakeBridgeProcess();
  let captured: NodeJS.ProcessEnv | undefined;
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    (_command, _args, options) => {
      captured = options.env;
      return fake;
    }
  );

  await client.startStdio(config(), {
    apiKey: "bridge-secret",
    providerCredentials: [
      { envVar: "anthropic_api_key", value: " anthropic-secret " },
      { envVar: "DEEPSEEK_API_KEY", value: "deepseek-secret" },
      // Neither of these may reach the spawned process: one has no value, the other is not a
      // legal env var name and would otherwise be injected verbatim.
      { envVar: "EMPTY_API_KEY", value: "   " },
      { envVar: "BAD NAME; export EVIL=1", value: "nope" }
    ]
  });

  assert.equal(captured?.ALYSIS_API_KEY, "bridge-secret");
  assert.equal(captured?.ANTHROPIC_API_KEY, "anthropic-secret");
  assert.equal(captured?.DEEPSEEK_API_KEY, "deepseek-secret");
  assert.equal(captured?.EMPTY_API_KEY, undefined);
  assert.equal(Object.keys(captured ?? {}).some((key) => key.includes(" ")), false);
  client.dispose();
});

test("stripApiKey removes provider-scoped keys as well as the generic one", async () => {
  const fake = new FakeBridgeProcess();
  let captured: NodeJS.ProcessEnv | undefined;
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    (_command, _args, options) => {
      captured = options.env;
      return fake;
    }
  );

  await client.startStdio(config(), {
    apiKey: "bridge-secret",
    stripApiKey: true,
    providerCredentials: [{ profile: "anthropic", envVar: "ANTHROPIC_API_KEY", value: "anthropic-secret" }]
  });

  assert.equal(captured?.ALYSIS_API_KEY, undefined);
  assert.equal(captured?.ANTHROPIC_API_KEY, undefined);
  assert.equal(Object.keys(captured ?? {}).some((key) => key.startsWith("ALYSIS_IDE_PROFILE_")), false);
  client.dispose();
});

test("IDE provider keys remain distinct when profiles declare the same environment variable", async () => {
  const processes: FakeBridgeProcess[] = [];
  let captured: NodeJS.ProcessEnv = {};
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    (_command, _args, options) => {
      captured = options.env;
      const fake = new FakeBridgeProcess();
      processes.push(fake);
      return fake;
    }
  );
  const options = {
    providerCredentials: [
      { profile: "openai-responses", envVar: "OPENAI_API_KEY", value: "first-key" },
      { profile: "default", envVar: "OPENAI_API_KEY", value: "second-key" }
    ]
  };
  const required = { credentialsRequired: true };
  try {
    await client.ensureStartedForProfile(config(), options, required);
    // Fixed expected names also cover the shared TypeScript/Python encoding contract.
    assert.equal(captured.ALYSIS_IDE_PROFILE_6F70656E61692D726573706F6E736573_API_KEY, "first-key");
    assert.equal(captured.ALYSIS_IDE_PROFILE_64656661756C74_API_KEY, "second-key");
    options.providerCredentials[0].value = "replacement-key";
    const replaced = await client.ensureStartedForProfile(config(), options, required);
    assert.equal(replaced.restarted, true);
    assert.equal(processes.length, 2);
    assert.equal(captured.ALYSIS_IDE_PROFILE_6F70656E61692D726573706F6E736573_API_KEY, "replacement-key");
    assert.equal((await client.ensureStartedForProfile(config(), options, required)).restarted, false);
  } finally {
    client.dispose();
  }
});

test("a bridge does not inherit another IDE's profile-key override", async () => {
  const name = "ALYSIS_IDE_PROFILE_64656661756C74_API_KEY";
  const previous = process.env[name];
  process.env[name] = "other-ide-key";
  let captured: NodeJS.ProcessEnv = {};
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    (_command, _args, options) => {
      captured = options.env;
      return new FakeBridgeProcess();
    }
  );
  try {
    await client.startStdio(config(), {});
    assert.equal(captured[name], undefined);
  } finally {
    client.dispose();
    if (previous === undefined) {
      delete process.env[name];
    } else {
      process.env[name] = previous;
    }
  }
});

test("storing a new provider key restarts the bridge instead of reusing a keyless process", async () => {
  const processes: FakeBridgeProcess[] = [];
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    () => {
      const fake = new FakeBridgeProcess();
      processes.push(fake);
      return fake;
    }
  );

  const required = { credentialsRequired: true };
  const first = await client.ensureStartedForProfile(config(), { apiKey: "bridge-secret" }, required);
  assert.equal(first.restarted, false);
  assert.equal(processes.length, 1);

  // Same generic key, newly added provider key: the running bridge cannot authenticate with a
  // credential it was never given, so it must be replaced rather than silently reused.
  const second = await client.ensureStartedForProfile(
    config(),
    { apiKey: "bridge-secret", providerCredentials: [{ envVar: "ANTHROPIC_API_KEY", value: "anthropic-secret" }] },
    required
  );
  assert.equal(second.restarted, true);
  assert.equal(processes.length, 2);

  // An unchanged credential set reuses the process.
  const third = await client.ensureStartedForProfile(
    config(),
    { apiKey: "bridge-secret", providerCredentials: [{ envVar: "ANTHROPIC_API_KEY", value: "anthropic-secret" }] },
    required
  );
  assert.equal(third.restarted, false);
  assert.equal(processes.length, 2);
  client.dispose();
});

test("AlysisBridgeClient strips inherited API keys when start options request no secrets", async () => {
  const previous = {
    ALYSIS_API_KEY: process.env.ALYSIS_API_KEY,
    OPENAI_API_KEY: process.env.OPENAI_API_KEY,
    GITHUB_TOKEN: process.env.GITHUB_TOKEN
  };
  process.env.ALYSIS_API_KEY = "inherited-secret";
  process.env.OPENAI_API_KEY = "inherited-openai-secret";
  process.env.GITHUB_TOKEN = "inherited-github-secret";
  const fake = new FakeBridgeProcess();
  let capturedEnv: NodeJS.ProcessEnv = {};
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    (_command, _args, options) => {
      capturedEnv = options.env;
      return fake;
    }
  );

  try {
    await client.startStdio(config(), { apiKey: "explicit-secret", stripApiKey: true });

    assert.equal(capturedEnv.ALYSIS_API_KEY, undefined);
    assert.equal(capturedEnv.OPENAI_API_KEY, undefined);
    assert.equal(capturedEnv.GITHUB_TOKEN, undefined);
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = value;
      }
    }
    client.dispose();
  }
});

test("AlysisBridgeClient refuses untrusted cliPath before forwarding API keys", async () => {
  let processFactoryCalled = false;
  let capturedApiKey: string | undefined;
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    (_command, _args, options) => {
      processFactoryCalled = true;
      capturedApiKey = options.env.ALYSIS_API_KEY;
      return new FakeBridgeProcess();
    }
  );

  await assert.rejects(
    client.startStdio(
      {
        ...config(),
        cliPath: "./malicious-alysis",
	        security: {
	          isWorkspaceTrusted: false,
	          ignoredWorkspaceSettings: [],
	          workspaceRoots: ["/workspace"],
	          cliPath: {
            value: "./malicious-alysis",
            source: "workspace",
            trusted: false,
            executionAllowed: false,
            apiKeyForwardingAllowed: false,
            reason: "Workspace-defined Alysis Code CLI path is ignored until Workspace Trust is granted."
          }
        }
      },
      { apiKey: "bridge-secret" }
    ),
    (error) => error instanceof ProtocolClientError && error.code === "cli_untrusted"
  );

  assert.equal(processFactoryCalled, false);
  assert.equal(capturedApiKey, undefined);
});

test("AlysisBridgeClient omits API keys when cliPath is not trusted defensively", async () => {
  const fake = new FakeBridgeProcess();
  let capturedApiKey: string | undefined;
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    (_command, _args, options) => {
      capturedApiKey = options.env.ALYSIS_API_KEY;
      return fake;
    }
  );

  await client.startStdio(
    {
      ...config(),
	      security: {
	        isWorkspaceTrusted: false,
	        ignoredWorkspaceSettings: [],
	        workspaceRoots: ["/workspace"],
	        cliPath: {
          value: "alysis",
          source: "default",
          trusted: true,
          executionAllowed: true,
          apiKeyForwardingAllowed: false
        }
      }
    },
    { apiKey: "bridge-secret" }
  );

  assert.equal(capturedApiKey, undefined);
  client.dispose();
});

test("AlysisBridgeClient strips inherited API keys when executable origin is untrusted", async () => {
  const previous = {
    ALYSIS_API_KEY: process.env.ALYSIS_API_KEY,
    OPENAI_API_KEY: process.env.OPENAI_API_KEY,
    GITHUB_TOKEN: process.env.GITHUB_TOKEN
  };
  process.env.ALYSIS_API_KEY = "inherited-secret";
  process.env.OPENAI_API_KEY = "inherited-openai-secret";
  process.env.GITHUB_TOKEN = "inherited-github-secret";
  const fake = new FakeBridgeProcess();
  let capturedEnv: NodeJS.ProcessEnv = {};
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    (_command, _args, options) => {
      capturedEnv = options.env;
      return fake;
    }
  );

  try {
    await client.startStdio({
      ...config(),
      security: {
        isWorkspaceTrusted: false,
        ignoredWorkspaceSettings: [],
        workspaceRoots: ["/workspace"],
        cliPath: {
          value: "alysis",
          source: "default",
          trusted: true,
          executionAllowed: true,
          apiKeyForwardingAllowed: false
        }
      }
    });

    assert.equal(capturedEnv.ALYSIS_API_KEY, undefined);
    assert.equal(capturedEnv.OPENAI_API_KEY, undefined);
    assert.equal(capturedEnv.GITHUB_TOKEN, undefined);
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = value;
      }
    }
    client.dispose();
  }
});

test("AlysisBridgeClient tracks no-secret launch profile and safely restarts for credentials", async () => {
  const fakes: FakeBridgeProcess[] = [];
  const capturedApiKeys: Array<string | undefined> = [];
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    (_command, _args, options) => {
      capturedApiKeys.push(options.env.ALYSIS_API_KEY);
      const fake = new FakeBridgeProcess();
      fakes.push(fake);
      return fake;
    }
  );

  const first = await client.ensureStartedForProfile(config(), { stripApiKey: true });
  assert.equal(first.profile.stripApiKey, true);
  assert.equal(first.profile.alysisApiKeyForwarded, false);
  assert.equal(first.profile.credentialCapable, false);

  const second = await client.ensureStartedForProfile(
    config(),
    { apiKey: "bridge-secret" },
    { credentialsRequired: true }
  );

  assert.equal(second.restarted, true);
  assert.equal(second.profile.stripApiKey, false);
  assert.equal(second.profile.alysisApiKeyForwarded, true);
  assert.equal(client.currentLaunchProfile()?.credentialCapable, true);
  assert.deepEqual(capturedApiKeys, [undefined, "bridge-secret"]);
  client.dispose();
});

test("AlysisBridgeClient never starts a replacement until the exact prior child exits", async () => {
  const fakes: FakeBridgeProcess[] = [];
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    () => {
      const fake = new FakeBridgeProcess({ autoExitOnKill: fakes.length !== 0 });
      fakes.push(fake);
      return fake;
    }
  );

  await client.ensureStartedForProfile(config(), { stripApiKey: true });
  const prior = fakes[0];
  const restarting = client.ensureStartedForProfile(
    config(),
    { apiKey: "bridge-secret" },
    { credentialsRequired: true }
  );

  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(prior.killCalls.length, 1);
  assert.equal(fakes.length, 1, "replacement spawned before exact prior-child exit");

  prior.exitCode = 0;
  prior.emit("exit", 0);
  const result = await restarting;

  assert.equal(result.restarted, true);
  assert.equal(fakes.length, 2);
  assert.equal(client.isRunning(), true);
  client.dispose();
});

test("AlysisBridgeClient refuses credential profile restart while a request is pending", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.ensureStartedForProfile(config(), { stripApiKey: true });

  const pending = client.sessionStatus("session-1").catch(() => undefined);
  await fake.waitForRequests(1);

  await assert.rejects(
    client.ensureStartedForProfile(config(), { apiKey: "bridge-secret" }, { credentialsRequired: true }),
    (error) => {
      assert.equal(error instanceof ProtocolClientError, true);
      assert.equal((error as ProtocolClientError).code, "bridge_profile_restart_blocked");
      assert.match(String((error as Error).message), /cannot be applied while work is active/);
      return true;
    }
  );
  fake.respond(0, sessionStatusPayload());
  await pending;
  client.dispose();
});

test("AlysisBridgeClient restarts an idle credential bridge when the API key changes", async () => {
  const capturedApiKeys: Array<string | undefined> = [];
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    (_command, _args, options) => {
      capturedApiKeys.push(options.env.ALYSIS_API_KEY);
      return new FakeBridgeProcess();
    }
  );

  const first = await client.ensureStartedForProfile(
    config(),
    { apiKey: "first-secret" },
    { credentialsRequired: true }
  );
  const second = await client.ensureStartedForProfile(
    config(),
    { apiKey: "rotated-secret" },
    { credentialsRequired: true }
  );

  assert.equal(first.restarted, false);
  assert.equal(second.restarted, true);
  assert.deepEqual(capturedApiKeys, ["first-secret", "rotated-secret"]);
  assert.equal(JSON.stringify(client.currentLaunchProfile()).includes("first-secret"), false);
  assert.equal(JSON.stringify(client.currentLaunchProfile()).includes("rotated-secret"), false);
  client.dispose();
});

test("AlysisBridgeClient drops a previously forwarded API key after it is removed", async () => {
  const previous = process.env.ALYSIS_API_KEY;
  delete process.env.ALYSIS_API_KEY;
  const capturedApiKeys: Array<string | undefined> = [];
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    (_command, _args, options) => {
      capturedApiKeys.push(options.env.ALYSIS_API_KEY);
      return new FakeBridgeProcess();
    }
  );

  try {
    await client.ensureStartedForProfile(
      config(),
      { apiKey: "temporary-secret" },
      { credentialsRequired: true }
    );
    const afterRemoval = await client.ensureStartedForProfile(
      config(),
      {},
      { credentialsRequired: true }
    );

    assert.equal(afterRemoval.restarted, true);
    assert.equal(afterRemoval.profile.alysisApiKeyForwarded, false);
    assert.deepEqual(capturedApiKeys, ["temporary-secret", undefined]);
  } finally {
    client.dispose();
    if (previous === undefined) {
      delete process.env.ALYSIS_API_KEY;
    } else {
      process.env.ALYSIS_API_KEY = previous;
    }
  }
});

test("AlysisBridgeClient refuses credential profile restart while jobs or approvals are active", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.ensureStartedForProfile(config(), { stripApiKey: true });

  const started = client.sendChat("session-1", "hello");
  await fake.waitForRequests(1);
  fake.respond(0, { session_id: "session-1", job_id: "job-1", status: "started" });
  await started;

  await assert.rejects(
    client.ensureStartedForProfile(config(), { apiKey: "bridge-secret" }, { credentialsRequired: true }),
    (error) => error instanceof ProtocolClientError && error.code === "bridge_profile_restart_blocked"
  );

  const completed = client.jobStatus("job-1");
  await fake.waitForRequests(2);
  fake.respond(1, { session_id: "session-1", job_id: "job-1", status: "completed" });
  await completed;

  fake.emitEvent(eventEnvelope({
    type: "prompt_for_input",
    payload: { kind: "approval", approval_id: "approval-1", prompt_id: "approval-1" },
    job_id: null,
    sequence: 2
  }));
  await assert.rejects(
    client.ensureStartedForProfile(config(), { apiKey: "bridge-secret" }, { credentialsRequired: true }),
    (error) => error instanceof ProtocolClientError && error.code === "bridge_profile_restart_blocked"
  );
  client.dispose();
});

test("AlysisBridgeClient refuses credential-required profile when executable origin cannot receive secrets", async () => {
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    () => new FakeBridgeProcess()
  );

  await assert.rejects(
    client.ensureStartedForProfile(
      {
        ...config(),
        security: {
          isWorkspaceTrusted: false,
          ignoredWorkspaceSettings: [],
          workspaceRoots: ["/workspace"],
          cliPath: {
            value: "alysis",
            source: "default",
            trusted: true,
            executionAllowed: true,
            apiKeyForwardingAllowed: false
          }
        }
      },
      { apiKey: "bridge-secret" },
      { credentialsRequired: true }
    ),
    (error) => error instanceof ProtocolClientError && error.code === "bridge_credentials_unavailable"
  );
});

test("AlysisBridgeClient fails startup cleanly when the process cannot spawn", async () => {
  const fake = new FakeBridgeProcess({ emitSpawn: false });
  const client = clientFor(fake);
  client.on("error", () => undefined);
  process.nextTick(() => fake.emit("error", new Error("spawn alysis ENOENT")));

  await assert.rejects(
    client.startStdio(config()),
    (error) => error instanceof ProtocolClientError && error.code === "bridge_start_failed"
  );
  assert.equal(client.isRunning(), false);
});

test("AlysisBridgeClient rejects pending requests when the bridge process errors", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  client.on("error", () => undefined);
  const resets: string[] = [];
  client.on("reset", (reason) => resets.push(reason));
  await client.startStdio(config());

  const pending = client.sessionStatus("session-1");
  await fake.waitForRequests(1);
  fake.emit("error", new Error("bridge crashed with Bearer abcdefgh1234567890"));

  await assert.rejects(pending, (error) => {
    assert.equal(error instanceof ProtocolClientError, true);
    assert.equal((error as ProtocolClientError).code, "bridge_process_error");
    assert.equal(error instanceof Error && error.message.includes("abcdefgh1234567890"), false);
    return true;
  });
  assert.equal(client.isRunning(), false);
  assert.deepEqual(resets, ["bridge_process_error"]);
  assert.deepEqual(fake.killCalls, ["SIGTERM"], "the exact failed bridge is terminated, not only forgotten");
});

test("AlysisBridgeClient waits for failed-process teardown before spawning a replacement", async () => {
  const first = new FakeBridgeProcess({ autoExitOnKill: false });
  let second: FakeBridgeProcess | undefined;
  let processFactoryCalls = 0;
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    () => {
      processFactoryCalls += 1;
      if (processFactoryCalls === 1) {
        return first;
      }
      second = new FakeBridgeProcess();
      return second;
    }
  );
  client.on("error", () => undefined);
  const resets: string[] = [];
  client.on("reset", (reason) => resets.push(reason));
  await client.startStdio(config());

  first.emit("error", new Error("broken bridge stream"));
  first.emit("error", new Error("the same child process failure reported twice"));
  assert.deepEqual(first.killCalls, ["SIGTERM"], "duplicate error signals share one teardown");

  let replacementStarted = false;
  const replacement = client.ensureStarted(config()).then(() => {
    replacementStarted = true;
  });
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(processFactoryCalls, 1, "replacement is blocked while the failed child remains alive");
  assert.equal(replacementStarted, false, "a duplicate child error is not treated as process exit");
  assert.equal(client.isRunning(), false);

  first.emit("exit", 1);
  await replacement;
  assert.equal(processFactoryCalls, 2);
  assert.equal(replacementStarted, true);
  assert.equal(client.isRunning(), true);
  assert.deepEqual(resets, ["bridge_process_error"]);
  assert.deepEqual(first.killCalls, ["SIGTERM"]);
  assert.deepEqual(second?.killCalls, []);
  client.dispose();
});

test("AlysisBridgeClient does not fence forever when startup observes an already-exited child", async () => {
  const first = new FakeBridgeProcess({ deferInitialize: true, autoExitOnKill: false });
  let processFactoryCalls = 0;
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    () => {
      processFactoryCalls += 1;
      return processFactoryCalls === 1 ? first : new FakeBridgeProcess();
    }
  );

  const initialStart = client.startStdio(config());
  await first.waitForInitializeRequests(1);
  // Reproduce the late-subscription ordering: the OS process is already terminal and both
  // lifecycle events have fired before the startup catch begins its teardown barrier.
  first.exitCode = 1;
  first.emit("exit", 1);
  first.emit("close", 1);
  await assert.rejects(initialStart, (error) =>
    error instanceof ProtocolClientError && error.code === "bridge_exited"
  );

  await client.startStdio(config());
  assert.equal(processFactoryCalls, 2);
  assert.deepEqual(first.killCalls, [], "an already-exited process is neither signalled nor awaited again");
  assert.equal(client.isRunning(), true);
  client.dispose();
});

test("AlysisBridgeClient ignores stale close events after a safe restart", async () => {
  let startCount = 0;
  let first: FakeBridgeProcess | undefined;
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    () => {
      startCount += 1;
      const fake = new FakeBridgeProcess({
        emitSpawn: false,
        autoExitOnKill: startCount !== 1
      });
      if (startCount === 1) {
        first = fake;
      }
      process.nextTick(() => fake.emit("spawn"));
      return fake;
    }
  );
  await client.startStdio(config());
  const resets: string[] = [];
  client.on("reset", (reason) => resets.push(reason));

  const restarted = client.restartIfSafe(config());
  process.nextTick(() => first?.emit("exit", 0));
  assert.equal(await restarted, true);
  first?.emit("close", 0);

  assert.equal(client.isRunning(), true);
  assert.deepEqual(resets, ["bridge_restarting"]);
  client.dispose();
});

test("AlysisBridgeClient handles stream errors instead of letting them kill the extension host", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  const errors: Error[] = [];
  const diagnostics: string[] = [];
  client.on("error", (error) => errors.push(error));
  client.on("stderr", (line) => diagnostics.push(line));
  await client.startStdio(config());

  // An "error" event with no listener throws out of the event loop and takes down the Extension
  // Host — and every other extension in the window — so each of these must be handled.
  assert.doesNotThrow(() => fake.stdout.emit("error", new Error("read ECONNRESET")));
  assert.doesNotThrow(() => fake.stderr.emit("error", new Error("diagnostics pipe closed")));

  assert.equal(client.isRunning(), false, "a broken stdout tears the bridge down deliberately");
  assert.equal(errors.length, 1);
  assert.equal(
    diagnostics.some((line) => /diagnostics stream failed/.test(line)),
    true,
    "a broken stderr is a diagnostic, not a bridge failure"
  );
});

test("AlysisBridgeClient gives each bridge method a deadline that matches its work", () => {
  assert.equal(requestTimeoutForMethod("initialize"), 10_000);
  assert.equal(requestTimeoutForMethod("bridge.shutdown"), 3_000);
  for (const method of [
    "session.list",
    "session.status",
    "job.status",
    "config.get",
    "config.set",
    "forge.assets.list",
    // Persona calls are config-shaped quick calls, so both live in the short bucket.
    "session.personas.list",
    "session.persona.set"
  ]) {
    assert.equal(requestTimeoutForMethod(method), 30_000, method);
  }
  for (const method of [
    "forge.plan",
    "forge.plan.result",
    "forge.review",
    "forge.review.result",
    "forge.execute",
    "code.review.result",
    "session.compact"
  ]) {
    assert.equal(requestTimeoutForMethod(method), 600_000, method);
  }
  assert.equal(requestTimeoutForMethod("chat.send"), 120_000, "unclassified methods keep the default");
});

test("AlysisBridgeClient keeps a long-running method alive past the default deadline", async (t) => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.startStdio(config());
  t.mock.timers.enable({ apis: ["setTimeout"] });

  const outcomes: unknown[] = [];
  const plan = client
    .forgePlan({ workspace: "/workspace", instruction: "Ship the release.", mode: "readonly" })
    .then(
      () => "resolved",
      (error: unknown) => error
    )
    .then((result) => {
      outcomes.push(result);
    });
  await fake.waitForRequests(1);

  // The old shared 120s default abandoned the caller here while the CLI kept planning (and spending).
  t.mock.timers.tick(150_000);
  await tick();
  assert.deepEqual(outcomes, [], "a plan is still in flight two minutes in");

  t.mock.timers.tick(600_000);
  await tick();
  await plan;
  assert.equal(outcomes.length, 1);
  const failure = outcomes[0] as ProtocolClientError;
  assert.equal(failure instanceof ProtocolClientError, true);
  assert.equal(failure.code, "request_timeout");
  t.mock.timers.reset();
  client.dispose();
});

test("AlysisBridgeClient fails the in-flight request when the bridge answers with a null id", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.startStdio(config());

  const pending = client.sessionStatus("session-1");
  await fake.waitForRequests(1);
  // The CLI answers anything it cannot parse (or anything above its 1 MiB limit) with id: null,
  // which never matches a pending vscode-N key.
  fake.errorWithoutId("request_too_large", "Request frame exceeds the 1 MiB bridge limit.");

  await assert.rejects(pending, (error) => {
    assert.equal(error instanceof ProtocolClientError, true);
    assert.equal((error as ProtocolClientError).code, "request_too_large");
    assert.match((error as Error).message, /1 MiB bridge limit/);
    return true;
  });
  client.dispose();
});

test("AlysisBridgeClient refuses to write a request above the bridge frame limit", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.startStdio(config());

  await assert.rejects(
    client.sendChat("session-1", "x".repeat(1_100_000)),
    (error) => {
      assert.equal(error instanceof ProtocolClientError, true);
      assert.equal((error as ProtocolClientError).code, "request_too_large");
      assert.match((error as Error).message, /frame limit/);
      return true;
    }
  );
  assert.equal(fake.requests.length, 0, "the oversized frame is never written to the bridge");
  client.dispose();
});

test("AlysisBridgeClient drops an oversized inbound frame and keeps correlating the stream", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  const diagnostics: string[] = [];
  const errors: Error[] = [];
  client.on("stderr", (line) => diagnostics.push(line));
  client.on("error", (error) => errors.push(error));
  await client.startStdio(config());

  const pending = client.sessionStatus("session-1");
  await fake.waitForRequests(1);
  fake.stdout.write(`{"padding":"${"a".repeat(9 * 1024 * 1024)}"}\n`);
  fake.respond(0, sessionStatusPayload({ model: "still-correlated" }));

  assert.equal((await pending).model, "still-correlated");
  assert.deepEqual(errors, []);
  assert.equal(diagnostics.some((line) => /oversized Alysis Code bridge frame/.test(line)), true);
  client.dispose();
});

test("AlysisBridgeClient truncates and rate limits forwarded CLI diagnostics", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  const diagnostics: string[] = [];
  client.on("stderr", (line) => diagnostics.push(line));
  await client.startStdio(config());

  fake.stderr.write("d".repeat(64 * 1024));
  await settle();
  assert.equal(diagnostics.length, 1);
  assert.equal(diagnostics[0].length < 9 * 1024, true, "one chunk cannot forward 64 KiB into the output channel");
  assert.match(diagnostics[0], /truncated \d+ characters/);

  for (let index = 0; index < 40; index += 1) {
    fake.stderr.write("noise ".repeat(2_000));
  }
  await settle();
  assert.equal(diagnostics.length < 30, true, "a runaway stderr stream is rate limited, not forwarded verbatim");
  client.dispose();
});

test("AlysisBridgeClient logs stray CLI stdout and escalates only a real protocol desync", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  const diagnostics: string[] = [];
  const errors: Error[] = [];
  client.on("stderr", (line) => diagnostics.push(line));
  client.on("error", (error) => errors.push(error));
  await client.startStdio(config());

  fake.stdout.write("WARNING: You are using pip version 21.0; consider upgrading.\n");
  await settle();
  assert.deepEqual(errors, [], "a single stray line is a diagnostic, not an error card in chat");
  assert.equal(diagnostics.some((line) => /Ignored unreadable Alysis Code bridge output/.test(line)), true);

  // A frame that announces itself as protocol output but does not parse is a genuine desync.
  fake.stdout.write("{\"protocol_version\":\"1\",\"id\":\"vscode-1\",\n");
  await settle();
  assert.equal(errors.length, 1);
  assert.equal((errors[0] as ProtocolClientError).code, "bridge_protocol_desync");

  for (let index = 0; index < 5; index += 1) {
    fake.stdout.write("more stray CLI output\n");
  }
  await settle();
  assert.equal(errors.length, 2, "a sustained run of unreadable frames escalates once, not per line");
  client.dispose();
});

test("AlysisBridgeClient serializes concurrent profile starts and never returns another caller's bridge", async () => {
  const capturedApiKeys: Array<string | undefined> = [];
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    (_command, _args, options) => {
      capturedApiKeys.push(options.env.ALYSIS_API_KEY);
      return new FakeBridgeProcess();
    }
  );
  await client.ensureStartedForProfile(config(), { stripApiKey: true });

  // stopProcess() clears the live process before its first await, so without a single-flight the
  // second caller spawns with ITS credentials while the first is still restarting — and the first
  // then reports the second caller's bridge as its own profile.
  const [alpha, beta] = await Promise.all([
    client.ensureStartedForProfile(config(), { apiKey: "alpha-secret" }, { credentialsRequired: true }),
    client.ensureStartedForProfile(config(), { apiKey: "beta-secret" }, { credentialsRequired: true })
  ]);

  assert.deepEqual(capturedApiKeys, [undefined, "alpha-secret", "beta-secret"]);
  assert.equal(alpha.restarted, true);
  assert.equal(beta.restarted, true);
  assert.equal(alpha.profile.credentialCapable, true);
  assert.equal(beta.profile.credentialCapable, true);
  client.dispose();
});

test("bridge process termination resolves taskkill from System32, never by bare name", () => {
  const resolved = taskkillExecutablePath({ SystemRoot: "D:\\Windows" });
  assert.equal(resolved, path.join("D:\\Windows", "System32", "taskkill.exe"));
  assert.equal(taskkillExecutablePath({}), path.join("C:\\Windows", "System32", "taskkill.exe"));
  assert.notEqual(taskkillExecutablePath({}), "taskkill.exe");
  assert.match(taskkillExecutablePath({}), /System32/);
});

test("passive catalog starts cannot replace the authenticated bridge during a provider upgrade", async () => {
  const children: FakeBridgeProcess[] = [];
  const forwarded: Array<string | undefined> = [];
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    (_command, _args, options) => {
      forwarded.push(options.env.DEEPSEEK_API_KEY);
      const child = new FakeBridgeProcess({ autoExitOnKill: children.length !== 0 });
      children.push(child);
      return child;
    }
  );
  try {
    await client.ensureStarted(config(), { stripApiKey: true });
    const authenticated = client.ensureStartedForProfile(config(), {
      providerCredentials: [{ profile: "deepseek", envVar: "DEEPSEEK_API_KEY", value: "deepseek-test-key" }]
    }, { credentialsRequired: true });
    await tick();
    assert.equal(children[0].killCalls.length, 1);
    const background = Array.from({ length: 8 }, (_, index) => index % 2 === 0
      ? client.ensureStarted(config(), { stripApiKey: true })
      : client.startStdio(config(), { stripApiKey: true }));
    const results = Promise.all([authenticated, ...background]);
    await tick();
    children[0].exitCode = 0;
    children[0].emit("exit", 0);
    const [ready] = await results;
    assert.equal(ready?.profile.credentialCapable, true);
    assert.deepEqual(forwarded, [undefined, "deepseek-test-key"]);
    assert.equal(client.currentLaunchProfile()?.credentialCapable, true);
  } finally {
    client.dispose();
  }
});

test("a passive startup in progress completes before an authenticated upgrade", async () => {
  const children: FakeBridgeProcess[] = [];
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    () => {
      const child = new FakeBridgeProcess({ deferInitialize: children.length === 0 });
      children.push(child);
      return child;
    }
  );
  try {
    const passive = client.ensureStarted(config(), { stripApiKey: true });
    await tick();
    await children[0].waitForInitializeRequests(1);
    const authenticated = client.ensureStartedForProfile(config(), { apiKey: "test-key" }, { credentialsRequired: true });
    const results = Promise.all([passive, authenticated]);
    children[0].respondToInitialize();
    const [, ready] = await results;
    assert.equal(ready.profile.credentialCapable, true);
    assert.equal(children.length, 2);
  } finally {
    client.dispose();
  }
});

test("an explicit restart keeps its credentials when a background startup overlaps teardown", async () => {
  const children: FakeBridgeProcess[] = [];
  const forwarded: Array<string | undefined> = [];
  const client = new AlysisBridgeClient(
    new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })),
    (_command, _args, options) => {
      forwarded.push(options.env.ALYSIS_API_KEY);
      const child = new FakeBridgeProcess({ autoExitOnKill: children.length !== 0 });
      children.push(child);
      return child;
    }
  );
  try {
    await client.startStdio(config(), { apiKey: "old-test-key" });
    const restarting = client.restartIfSafe(config(), { apiKey: "new-test-key" });
    await tick();
    assert.equal(children[0].killCalls.length, 1);
    const refreshing = client.ensureStarted(config(), { stripApiKey: true });
    const results = Promise.all([restarting, refreshing]);
    await tick();
    children[0].exitCode = 0;
    children[0].emit("exit", 0);
    assert.equal((await results)[0], true);
    assert.deepEqual(forwarded, ["old-test-key", "new-test-key"]);
    assert.equal(client.currentLaunchProfile()?.credentialCapable, true);
  } finally {
    client.dispose();
  }
});

function settle(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 25));
}

function tick(): Promise<void> {
  return new Promise((resolve) => setImmediate(resolve));
}

test("AlysisBridgeClient sends persona requests on the wire contract and parses the results", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.startStdio(config());

  const list = client.sessionPersonasList({ session_id: "session-1" });
  await fake.waitForRequests(1);
  assert.equal(fake.requests[0].method, "session.personas.list");
  assert.deepEqual(fake.requests[0].params, { session_id: "session-1" });
  fake.respond(0, {
    enabled: true,
    active: "code",
    active_source: "user",
    personas: [
      {
        name: "architect",
        description: "Planning persona; writes markdown only.",
        default_exec_mode: "review",
        model_role: "planner",
        source_scope: "builtin",
        allow_write_globs: ["**/*.md"]
      }
    ]
  });
  const personas = await list;
  assert.equal(personas.active, "code");
  assert.equal(personas.personas[0].name, "architect");
  assert.equal(personas.personas[0].default_exec_mode, "review");

  const set = client.sessionPersonaSet({ session_id: "session-1", persona: "architect" });
  await fake.waitForRequests(2);
  assert.equal(fake.requests[1].method, "session.persona.set");
  assert.deepEqual(fake.requests[1].params, { session_id: "session-1", persona: "architect" });
  fake.respond(1, { persona: "architect", effective_mode: "review", model_role: "planner", changed: true });
  const outcome = await set;
  assert.deepEqual(outcome, { persona: "architect", effective_mode: "review", model_role: "planner", changed: true });
  client.dispose();
});

test("AlysisBridgeClient rejects persona results that violate the closed schema", async () => {
  const fake = new FakeBridgeProcess();
  const client = clientFor(fake);
  await client.startStdio(config());

  const list = client.sessionPersonasList({ session_id: "session-1" });
  await fake.waitForRequests(1);
  fake.respond(0, { enabled: "yes", personas: [] });
  await assert.rejects(list, /enabled/);

  const set = client.sessionPersonaSet({ session_id: "session-1", persona: "architect" });
  await fake.waitForRequests(2);
  fake.respond(1, { persona: "architect", effective_mode: "fullaccess", changed: true });
  await assert.rejects(set, /effective_mode/);
  client.dispose();
});

function clientFor(fake: FakeBridgeProcess): AlysisBridgeClient {
  return new AlysisBridgeClient(new CliDiscovery(async () => ({ stdout: "", stderr: "", exitCode: 0 })), () => fake);
}

function config() {
  return {
    cliPath: "alysis",
    defaultMode: "readonly" as const,
    defaultModel: "",
    baseUrl: "",
    provider: "",
    transport: "stdio" as const,
    sandboxProfile: "default" as const,
    forgeExecuteMaxSteps: undefined,
    forgeExecuteNoLog: false,
    showStatusBar: true,
    autoStartBridge: false,
    enableForge: true
  };
}

function eventEnvelope(overrides: Partial<ProtocolEventEnvelope>): ProtocolEventEnvelope {
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

function sessionStatusPayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    session_id: "session-1",
    workspace_root: "/workspace",
    mode: "review",
    closed: false,
    active_job: null,
    model: "test-model",
    base_url: "https://provider.example/v1",
    temperature: 0.2,
    stream: false,
    max_steps: 50,
    no_log: false,
    yes: false,
    subagents_enabled: true,
    active_workdir: "/workspace",
    active_workdir_relpath: ".",
    effective_verification_commands: ["pytest -q"],
    message_count: 2,
    pending_approvals: 0,
    ...overrides
  };
}

function terminalShowPayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    session_id: "session-1",
    supported: true,
    available: true,
    process_id: "proc-1",
    status: "running",
    exit_code: null,
    failure_reason: null,
    lines: [{ seq: 1, stream: "stdout", text: "ok", ts: "2026-05-19T10:00:00.000Z" }],
    line_count: 1,
    next_seq: 1,
    dropped_lines: 0,
    runtime_s: 1,
    started_at: "2026-05-19T10:00:00.000Z",
    total_bytes: 2,
    bytes: 2,
    max_lines: 200,
    max_bytes: 32768,
    truncated: false,
    redacted: true,
    secret_values_included: false,
    arbitrary_shell_execution: false,
    interactive_pty_streaming: false,
    terminals: [],
    count: 0,
    ...overrides
  };
}

function structuredQuestionPayload(
  overrides: Record<string, unknown> = {}
): Record<string, unknown> {
  return {
    session_id: "session-1",
    question_set_id: "questions-1",
    status: "pending",
    revision: 1,
    questions: [],
    answers: [],
    created_at: 1,
    updated_at: 1,
    expires_at: 301,
    terminal_at: null,
    resolution_attempts: 0,
    ...overrides
  };
}

function browserStatusPayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    session_id: "session-1",
    browser_session_id: "browser-session-12345678",
    product: "chromium",
    state: "running",
    created_at: 1_700_000_000,
    network_scope: "public",
    allow_local_destinations: false,
    active_url: null,
    artifact_count: 0,
    ...overrides
  };
}

function oauthFlowPayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    flow_id: "flow-1",
    server_id: "demo",
    kind: "authorization_code",
    state: "pending",
    created_at: 1,
    updated_at: 1,
    expires_at: 301,
    terminal_at: null,
    error_code: null,
    tokens_in_protocol_params: false,
    authorization_code_in_protocol: false,
    secret_values_included: false,
    ...overrides
  };
}

function mcpServerLifecyclePayload(
  overrides: Record<string, unknown> = {}
): Record<string, unknown> {
  return {
    session_id: "session-1",
    server_id: "alpha",
    transport: "stdio",
    enabled: true,
    connection_state: "connected",
    connected: true,
    generation: 1,
    catalog_initialized: true,
    exposed_tool_count: 2,
    snapshotted_resource_count: 1,
    prompt_snapshot_loaded: true,
    snapshotted_prompt_count: 1,
    secret_values_included: false,
    ...overrides
  };
}

function forgePlanPayload(): Record<string, unknown> {
  return {
    plan_id: "plan-1",
    session_id: "session-1",
    job_id: null,
    status: "planned",
    source: "active_memory",
    created_session: false,
    project_goal: "Prepare Forge protocol foundation.",
    summary: "Prepare Forge protocol foundation.",
    warnings: [],
    incomplete: false,
    tasks: [
      forgeTaskPayload()
    ],
    artifacts: [
      {
        kind: "forge_plan_json",
        artifact_id: "forge_plan_1:plan/plan.json",
        path: "plan/plan.json"
      }
    ],
    plan_artifact_id: "forge_plan_1:plan/plan.json",
    plan_markdown_artifact_id: "forge_plan_1:plan/PLAN.md"
  };
}

function forgePlanStatePayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    ...forgePlanPayload(),
    ide_revision: 1,
    assistant: {
      instruction: "Use scoped edits.",
      updated_at: null,
      source: "ide_protocol"
    },
    goal: "Prepare Forge protocol foundation.",
    validation: { ok: true },
    changed: false,
    audit: { secret_values_included: false },
    ...overrides
  };
}

function forgeTaskPayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    task_id: "T01",
    title: "Prepare Forge protocol foundation.",
    objective: "Prepare Forge protocol foundation.",
    file_scope: {
      estimated_files: ["src/ide"],
      write_scope: ["src/ide"]
    },
    acceptance_criteria: ["Protocol schema is stable."],
    verification_commands: ["pytest tests/test_ide_stdio_bridge.py"],
    risk_notes: ["Low risk."],
    dependencies: [],
    order: 1,
    scope_unknown_reason: "",
    warnings: [],
    status: "planned",
    ...overrides
  };
}

function forgeAssetRecordPayload(): Record<string, unknown> {
  return {
    id: "asset_1",
    title: "Spec",
    description: "",
    kind: "text",
    mime: "text/markdown",
    original_filename: "spec.md",
    size_bytes: 42,
    sha256: "abc123",
    stored_path: ".alysis/runs/plan-1/assets/raw/asset_1/spec.md",
    extracted_text_path: ".alysis/runs/plan-1/assets/raw/asset_1/spec.md",
    thumbnail_path: null,
    pinned: false,
    added_at: "2026-01-01T00:00:00Z",
    added_by: { phase: "ide_protocol" },
    deleted_at: null,
    comprehension_status: "pending",
    comprehension_current_version: null
  };
}

function forgeAssetEntryPayload(): Record<string, unknown> {
  return {
    record: forgeAssetRecordPayload(),
    comprehension_status: "pending",
    comprehension_source: null,
    comprehension_summary_preview: "",
    detected_language: null
  };
}

function forgeAssetDetailPayload(): Record<string, unknown> {
  return {
    record: forgeAssetRecordPayload(),
    comprehension_status: "pending",
    comprehension: null,
    versions: [],
    extracted_text_preview: ""
  };
}

function forgeExecutePreviewPayload(): Record<string, unknown> {
  return {
    session_id: "session-1",
    plan_id: "plan-1",
    selected_task_ids: ["T01"],
    execution_mode_requested: "readonly",
    workspace_trust_required: false,
    workspace_trusted: false,
    estimated_file_scopes: [
      {
        task_id: "T01",
        title: "Prepare Forge protocol foundation.",
        estimated_files: ["src/ide"],
        write_scope: ["src/ide"],
        scope_unknown_reason: ""
      }
    ],
    verification_commands: [
      {
        task_id: "T01",
        commands: ["pytest tests/test_ide_stdio_bridge.py"],
        source: "task",
        missing_reason: ""
      }
    ],
    required_approvals: [
      {
        kind: "fs_write",
        task_id: "T01",
        reason: "Forge execution may write the scoped task files.",
        scope: { type: "exact_file_set" },
        allow_for_session_scope: { type: "exact_file_set" },
        allow_for_session_supported: true
      }
    ],
    approval_scopes_safe: true,
    sandbox_profile: {
      requested: "default",
      supported: true,
      available: true,
      diagnostic: null
    },
    known_risks: ["Real execution is not wired."],
    missing_prerequisites: [],
    preview_ready: true,
    real_execution_supported: false,
    unsupported_reason: "Forge runtime execution is not wired.",
    active_cancellation_supported: true,
    cancellation: { supported: true, kind: "cooperative_checkpoint", hard_interrupt: false },
    max_steps: 5,
    no_log: true,
    subagents_supported: false,
    subagents_enabled: false,
    subagents_policy: "disabled_for_ide_forge_execute_v1",
    next_recommended_action: "Review the preview.",
    status: "preview"
  };
}

function durableSwarmStatusPayload(
  overrides: Record<string, unknown> = {}
): Record<string, unknown> {
  return {
    job_id: "job-swarm-1",
    state: "succeeded",
    revision: 3,
    attempts: 1,
    resume_count: 0,
    created_at: 1_700_000_000,
    updated_at: 1_700_000_100,
    started_at: 1_700_000_010,
    terminal_at: 1_700_000_100,
    lease_expires_at: null,
    result_available: true,
    error_code: null,
    error_summary: null,
    usage: {
      calls: 2,
      input_tokens: 10,
      output_tokens: 5,
      cached_input_tokens: 3,
      total_tokens: 15
    },
    resumable: false,
    ...overrides
  };
}

class FakeBridgeProcess extends EventEmitter implements BridgeProcess {
  public readonly stdin = new PassThrough();
  public readonly stdout = new PassThrough();
  public readonly stderr = new PassThrough();
  public readonly requests: Array<{ id: string | number | null; method: string; params: Record<string, unknown> }> = [];
  public initializeRequests = 0;
  public exitCode: number | null = null;
  public signalCode: NodeJS.Signals | null = null;
  public readonly killCalls: Array<NodeJS.Signals | number | undefined> = [];
  private requestWaiters: Array<() => void> = [];
  private initializeWaiters: Array<() => void> = [];
  private pendingInitializeIds: Array<string | number | null> = [];

  public constructor(
    private readonly options: {
      emitSpawn?: boolean;
      autoExitOnKill?: boolean;
      deferInitialize?: boolean;
      initializePayload?: Record<string, unknown>;
      sessionList?: Array<Record<string, unknown>>;
      sessionListError?: boolean;
      sessionCancelResult?: Record<string, unknown>;
      jobStatusResults?: Array<Record<string, unknown>>;
    } = {}
  ) {
    super();
    this.stdin.on("data", (chunk) => {
      for (const line of chunk.toString("utf8").split("\n")) {
        if (!line.trim()) {
          continue;
        }
        const request = JSON.parse(line) as { id: string | number | null; method: string; params: Record<string, unknown> };
        if (request.method === "initialize") {
          this.initializeRequests += 1;
          this.pendingInitializeIds.push(request.id);
          this.initializeWaiters.splice(0).forEach((resolve) => resolve());
          if (this.options.deferInitialize) {
            continue;
          }
          this.pendingInitializeIds.shift();
          this.writeResponse(request.id, this.options.initializePayload ?? healthPayload());
          continue;
        }
        if (request.method === "session.list" && this.options.sessionListError) {
          this.requests.push(request);
          this.writeError(request.id, "bridge_unavailable", "session list failed");
          continue;
        }
        if (request.method === "session.list" && this.options.sessionList) {
          this.requests.push(request);
          this.writeResponse(request.id, { sessions: this.options.sessionList });
          continue;
        }
        if (request.method === "session.cancel" && this.options.sessionCancelResult) {
          this.requests.push(request);
          this.writeResponse(request.id, this.options.sessionCancelResult);
          continue;
        }
        if (request.method === "job.status" && this.options.jobStatusResults) {
          this.requests.push(request);
          this.writeResponse(
            request.id,
            this.options.jobStatusResults.shift() ?? {
              job_id: String(request.params?.job_id ?? "job-unknown"),
              session_id: "session-unknown",
              status: "completed"
            }
          );
          continue;
        }
        this.requests.push(request);
      }
      this.requestWaiters.splice(0).forEach((resolve) => resolve());
    });
    if (this.options.emitSpawn !== false) {
      process.nextTick(() => this.emit("spawn"));
    }
  }

  public kill(signal?: NodeJS.Signals | number): boolean {
    this.killCalls.push(signal);
    if (this.options.autoExitOnKill !== false) {
      this.exitCode = 0;
      this.emit("exit", 0);
    }
    return true;
  }

  public respond(index: number, result: Record<string, unknown>): void {
    this.writeResponse(this.requests[index].id, result);
  }

  public error(index: number, code: string, message: string): void {
    this.stdout.write(
      JSON.stringify({
        protocol_version: PROTOCOL_VERSION,
        id: this.requests[index].id,
        ok: false,
        error: { code, message }
      }) + "\n"
    );
  }

  /** The shape the bridge returns for an unparseable or oversized request: an error with no id. */
  public errorWithoutId(code: string, message: string): void {
    this.writeError(null, code, message);
  }

  public emitEvent(event: ProtocolEventEnvelope): void {
    this.stdout.write(JSON.stringify(event) + "\n");
  }

  public async waitForRequests(count: number): Promise<void> {
    while (this.requests.length < count) {
      await new Promise<void>((resolve) => this.requestWaiters.push(resolve));
    }
  }

  public async waitForInitializeRequests(count: number): Promise<void> {
    while (this.initializeRequests < count) {
      await new Promise<void>((resolve) => this.initializeWaiters.push(resolve));
    }
  }

  public respondToInitialize(result: Record<string, unknown> = healthPayload()): void {
    const id = this.pendingInitializeIds.shift();
    if (id === undefined) {
      throw new Error("No deferred initialize request is pending.");
    }
    this.writeResponse(id, result);
  }

  private writeResponse(id: string | number | null, result: Record<string, unknown>): void {
    this.stdout.write(
      JSON.stringify({
        protocol_version: PROTOCOL_VERSION,
        id,
        ok: true,
        result
      }) + "\n"
    );
  }

  private writeError(id: string | number | null, code: string, message: string): void {
    this.stdout.write(
      JSON.stringify({
        protocol_version: PROTOCOL_VERSION,
        id,
        ok: false,
        error: { code, message }
      }) + "\n"
    );
  }
}

function healthPayload(): Record<string, unknown> {
  return {
    ok: true,
    name: "alysis-ide-bridge",
    alysis_version: "0.1.4",
    protocol_version: PROTOCOL_VERSION,
    capabilities: {
      protocol_version: PROTOCOL_VERSION,
      methods: [...REQUIRED_BRIDGE_METHODS],
      events: ["message_delta", "tool_call_started"],
      modes: ["readonly", "review", "auto"],
      transport: "stdio-jsonl",
      features: {
        structured_surface_events: true
      }
    }
  };
}

function healthPayloadWithMethods(methods: string[]): Record<string, unknown> {
  const payload = healthPayload();
  const capabilities = payload.capabilities as Record<string, unknown>;
  return {
    ...payload,
    capabilities: {
      ...capabilities,
      methods: [...REQUIRED_BRIDGE_METHODS, ...methods]
    }
  };
}
