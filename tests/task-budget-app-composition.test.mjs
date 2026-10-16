import assert from 'node:assert/strict';
import test from 'node:test';
import http from 'node:http';
import { createHash } from 'node:crypto';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { join } from 'node:path';

const app = (process.env.ALYSIS_TASK_BUDGET_APP ?? process.env.ALYSIS_TASK_BUDGET_APP);
const python = (process.env.ALYSIS_APP_RUNTIME_PYTHON ?? process.env.ALYSIS_APP_RUNTIME_PYTHON);
// Cross-repository harness: it drives the desktop app's real bridge client, so it only runs
// when ALYSIS_TASK_BUDGET_APP names an app checkout and ALYSIS_APP_RUNTIME_PYTHON a Python.
if (!app || !python) {
  test('task budget app composition (set ALYSIS_TASK_BUDGET_APP and ALYSIS_APP_RUNTIME_PYTHON)', { skip: true }, () => {});
} else {
const { setup } = await import(pathToFileURL(join(app, 'tests/helpers/task-budget-controller-fixture.mjs')));
const { IdeRuntimeBridge } = await import(pathToFileURL(join(app, 'electron/runtime/ideRuntimeBridge.ts')));
const sha = value => createHash('sha256').update(value).digest('hex');
const fixture = fileURLToPath(new URL('./fixtures/task_budget_ide.py', import.meta.url));

for (const mode of ['success', 'retry', 'cancel-before-ack', 'lost-ack', 'renew', 'deadline', 'autonomous', 'mutation-renew', 'mutation-renew-no-read']
  .filter(mode => !(process.env.ALYSIS_TASK_BUDGET_MODE ?? process.env.ALYSIS_TASK_BUDGET_MODE) || mode === (process.env.ALYSIS_TASK_BUDGET_MODE ?? process.env.ALYSIS_TASK_BUDGET_MODE))) test(`real Python IDE / durable app / actual HTTP: ${mode}`, { timeout: 30000 }, async t => {
  let f, requests = 0, permits = 0, settlements = 0, renewals = 0, answers = 0;
  const mutationMode = mode.startsWith('mutation-renew');
  const server = http.createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const payload = JSON.parse(Buffer.concat(chunks).toString());
    requests++;
    assert.equal(f.store.read(f.task.taskId).budgetEnvelope.usage.modelCalls, requests, 'real store must contain charge before actual HTTP');
    assert.equal(permits, requests); assert.equal(settlements, requests - 1);
    assert.equal(req.url, '/v1/chat/completions');
    if (mode === 'retry' && requests === 1) { res.writeHead(429, { 'content-type': 'application/json' }); res.end(JSON.stringify({ error: { message: 'generated temporary rate limit' } })); return; }
    if (mode === 'deadline') return; // Hold the actual socket until the bounded provider client closes it.
    if (mode === 'renew' && requests === 1) { await new Promise(resolve => setTimeout(resolve, 1200)); f.advance(1500); }
    answers++;
    const call = answers <= (mode === 'autonomous' ? 65 : mode === 'mutation-renew-no-read' ? 3 : mutationMode ? 4 : 1);
    const mutation = mutationMode && [2, 3].includes(answers);
    const message = call ? { role: 'assistant', content: null, tool_calls: [{ id: `call_generated_${answers}`, type: 'function', function: {
      name: mutation ? 'computer_action' : 'computer_state', arguments: mutation ? JSON.stringify({ action: 'press', element_id: 'next' }) : '{}' } }] }
      : { role: 'assistant', content: 'The generated fixture was observed.' };
    assert.ok(Array.isArray(payload.messages));
    res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify({ id: `generated_${requests}`, choices: [{ index: 0, finish_reason: call ? 'tool_calls' : 'stop', message }], usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } }));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve)); t.after(() => server.close());
  const endpoint = `http://127.0.0.1:${server.address().port}/v1`;
  const bridge = new IdeRuntimeBridge({ executable: python, args: [fixture], env: { ...process.env, ALYSIS_BUDGET_FIXTURE_ENDPOINT: endpoint }, requestTimeoutMs: 8000, handshakeTimeoutMs: 10000 });
  t.after(() => bridge.shutdown()); await bridge.start();
  const accountId = sha('alysis-route-scope:v1:generated-test-key');
  const endpointDigest = `sha256:${sha(`computer-use-endpoint-v1\0${endpoint}`)}`;
  const routeFingerprint = `sha256:${sha(`computer-use-route-v1\0${JSON.stringify({ account_id: accountId, auth_source: 'override', endpoint_digest: endpointDigest, model: 'gpt-4.1', profile: 'default', version: 1 })}`)}`;
  f = await setup(t, { owner: bridge,
    ...(mode === 'renew' ? { plan: { lease: { maxDurationMs: 1000, maxMutations: 20 } } } : {}),
    ...(mutationMode ? { plan: { lease: { maxDurationMs: 300000, maxMutations: 1 } } } : {}),
    creation: { routeDigest: routeFingerprint, providerBinding: { profile: 'default', accountId, model: 'gpt-4.1', endpointDigest },
      ...(mode === 'deadline' ? { budget: { maxActions: 200, maxDurationMs: 1500 } } : {}) } });
  const events = [], errors = [], hostOutcomes = [];
  bridge.onDisconnect(value => { if (!value.expected) console.error("fixture disconnect", value, errors, events.slice(-3)); });
  bridge.onEvent(event => events.push(event));
  bridge.onTaskBudgetRequest(pending => { void (async () => {
    if (mode === 'cancel-before-ack' && pending.request.kind === 'attempt') { f.abort.abort(); await bridge.cancelSession(f.creation.conversationId); return; }
    const response = await f.runtime.handleIdeRequest(f.run, pending, { owner: bridge, assertCurrent: () => bridge.assertTaskBudgetRequest(pending) }, f.controller);
    if (pending.request.kind === 'attempt') permits++; else if (pending.request.kind === 'settled') settlements++; else renewals++;
    if (mode === 'lost-ack' && pending.request.kind === 'attempt') { await bridge.cancelSession(f.creation.conversationId); return; }
    await bridge.respondTaskBudget(pending, response);
  })().catch(error => errors.push(error)); });
  bridge.onComputerUseRequest(pending => { void (async () => {
    const response = await f.controller.handleRequest(f.binding.operationId, pending);
    hostOutcomes.push({ tool: pending.toolName, phase: pending.arguments.approval_phase, ok: response.ok,
      ...(response.ok ? {} : { code: response.error.code }) });
    await bridge.respondToComputerUse({ ...pending, ...response });
  })().catch(error => errors.push(error)); });
  const session = await bridge.createSession({ workspace: f.root, mode: 'review', sessionId: f.creation.conversationId, model: 'gpt-4.1', baseUrl: endpoint,
    sensitiveMedia: true, computerUseHost: f.binding, computerUseDestination: { version: 1, model: 'gpt-4.1', profile: 'default', authSource: 'override', accountId, endpointDigest, routeFingerprint } });
  const job = await bridge.sendChat({ sessionId: session.sessionId, message: 'Observe the generated test fixture.' });
  const deadline = Date.now() + 15000; let status;
  do { status = await bridge.getJobStatus(job.jobId, 2000); if (['completed','failed','cancelled'].includes(status.status)) break; await new Promise(resolve => setTimeout(resolve, 25)); } while (Date.now() < deadline);
  assert.deepEqual(errors, []);
  if (['cancel-before-ack', 'lost-ack'].includes(mode)) {
    assert.equal(requests, 0); assert.equal(settlements, 0); assert.equal(status.status, 'cancelled');
    assert.equal(f.store.read(f.task.taskId).budgetEnvelope.usage.modelCalls, mode === 'lost-ack' ? 1 : 0);
    if (mode === 'lost-ack') assert.throws(() => f.runtime.controller.beginVerification(f.task.operationId));
  } else if (mode === 'deadline') {
    assert.equal(requests, 1); assert.equal(permits, 1); assert.ok(settlements <= 1); assert.notEqual(status.exitCode, 0);
  } else if (mode === 'mutation-renew-no-read') {
    assert.equal(status.status, 'completed'); assert.equal(status.exitCode, 1, 'ignoring forced fresh state must not return a successful turn');
    assert.equal(requests, 4); assert.equal(permits, 4); assert.equal(settlements, 4); assert.equal(renewals, 1);
    assert.equal(f.counts().actions, 1); assert.equal(f.counts().reads, 3, 'only scope setup, original observation and private renewal verification ran');
    assert.equal(hostOutcomes.filter(result => result.tool === 'computer_state').length, 1);
    assert.equal(f.store.read(f.task.taskId).budgetEnvelope.lease.ordinal, 2);
    assert.equal(f.store.read(f.task.taskId).terminalPredicate, null);
  } else {
    assert.equal(status.status, 'completed', JSON.stringify(status)); assert.equal(status.exitCode, 0,
      JSON.stringify({ status, requests, renewals, counts: f.counts(), hostOutcomes, lastEvents: events.slice(-4) }));
    const expected = mode === 'retry' ? 3 : mode === 'autonomous' ? 66 : mode === 'mutation-renew' ? 5 : 2;
    assert.equal(requests, expected); assert.equal(permits, expected); assert.equal(settlements, expected);
    assert.equal(f.store.read(f.task.taskId).budgetEnvelope.usage.modelCalls, expected);
    assert.equal(renewals, ['renew', 'mutation-renew'].includes(mode) ? 1 : 0);
    if (mode === 'mutation-renew') {
      assert.equal(f.counts().actions, 1, 'the refused second action is not replayed after renewal');
      assert.equal(f.store.read(f.task.taskId).budget.usedActions, 1);
      assert.equal(f.store.read(f.task.taskId).budget.maxActions, 200);
      assert.equal(f.store.read(f.task.taskId).budgetEnvelope.lease.ordinal, 2);
    }
  }
  assert.equal(events.some(event => event.type === 'task_budget_request'), false);
});
}
