'use strict';
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const test = require('node:test');
const {runProbe} = require('./windows_native_https_probe.cjs');
const TAG = 'ALYSIS_HTTPS_STAGE ';
const PRIVATE = 'PRIVATE_TOKEN_198.51.100.77_https://secret.invalid/?password=hidden';

function fixture(synchronousError = null) {
  const request = new EventEmitter(), socket = new EventEmitter();
  const out = [], err = [], exits = [], calls = [];
  let now = 0, timer, cleared = false, receive;
  runProbe({
    get: (...args) => {
      calls.push(args);
      if (synchronousError) throw synchronousError;
      receive = args[1];
      return request;
    },
    clock: () => now,
    setTimer: (callback, milliseconds) => {
      assert.equal(milliseconds, 25000);
      timer = callback;
      return 42;
    },
    clearTimer: handle => { assert.equal(handle, 42); cleared = true; },
    stdout: line => out.push(line), stderr: line => err.push(line), exit: code => exits.push(code),
  });
  request.emit('socket', socket);
  return {request, socket, out, err, exits, calls,
    tick: value => { now = value; },
    timeout: () => { now = 25000; timer(); },
    respond: (statusCode = 200) => {
      const response = new EventEmitter();
      response.statusCode = statusCode;
      receive(response);
      return response;
    },
    result: () => {
      assert.equal(err.length, 1);
      assert.ok(err[0].startsWith(TAG));
      assert.ok(err[0].length < 512);
      assert.ok(!JSON.stringify({out, err, exits}).includes(PRIVATE));
      assert.equal(cleared, true);
      return JSON.parse(err[0].slice(TAG.length));
    },
  };
}
function connected(f) {
  f.tick(2); f.socket.emit('lookup', null, PRIVATE, 4, PRIVATE);
  f.tick(4); f.socket.emit('connect');
  f.tick(6); f.socket.emit('secureConnect');
}
function valid(response) {
  response.emit('data', '{"name":"is-number","version":"7.0.0"}');
  response.emit('end');
}

test('successful fixed HTTPS request retains default TLS and emits bounded stage evidence', () => {
  const f = fixture();
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].length, 2); // No custom TLS/proxy/auth/redirect options.
  assert.equal(f.calls[0][0], 'https://registry.npmjs.org/is-number/7.0.0');
  connected(f); f.tick(8);
  const response = f.respond();
  f.tick(10); valid(response);
  assert.deepEqual(f.exits, [0]);
  assert.deepEqual(f.out, ['PUBLIC_HTTPS_OK']);
  assert.deepEqual(f.result(), {last_stage: 'end', dns_ms: 2, connect_ms: 4, tls_ms: 6,
    http_ms: 8, end_ms: 10, elapsed_ms: 10, http_status: 200, exit_code: 0, error_code: null});
  f.timeout();
  f.request.emit('error', {code: PRIVATE, message: PRIVATE});
  assert.equal(f.err.length, 1); assert.deepEqual(f.exits, [0]); assert.equal(f.calls.length, 1);
});

for (const lastStage of ['start', 'dns', 'connect', 'tls', 'http']) {
  test('hang at ' + lastStage + ' fails at the unchanged overall deadline without retry', () => {
    const f = fixture();
    const stages = ['dns', 'connect', 'tls'];
    for (const stage of stages) {
      if (lastStage === 'start') break;
      f.tick(2 + stages.indexOf(stage));
      f.socket.emit({dns: 'lookup', connect: 'connect', tls: 'secureConnect'}[stage], null, PRIVATE);
      if (stage === lastStage) break;
    }
    let response;
    if (lastStage === 'http') { f.tick(5); response = f.respond(); response.emit('data', '{"partial":"' + PRIVATE); }
    f.timeout();
    assert.deepEqual(f.exits, [31]); assert.deepEqual(f.out, ['NETWORK_BLOCKED']);
    const evidence = f.result();
    assert.equal(evidence.last_stage, lastStage); assert.equal(evidence.elapsed_ms, 25000);
    assert.equal(evidence.error_code, 'ETIMEDOUT'); assert.equal(evidence.end_ms, null);
    if (response) response.emit('end');
    assert.equal(f.calls.length, 1); assert.equal(f.err.length, 1);
  });
}

for (const code of ['EACCES', 'EPERM', 'ENOTFOUND', 'EAI_AGAIN', 'ETIMEDOUT', 'CERT_HAS_EXPIRED', PRIVATE]) {
  test('request error category ' + (code === PRIVATE ? 'unknown' : code) + ' remains failure and redacts details', () => {
    const f = fixture();
    f.tick(3);
    f.request.emit('error', {code, message: PRIVATE, address: PRIVATE, stack: PRIVATE});
    const blocked = ['EACCES', 'EPERM', 'ENOTFOUND', 'EAI_AGAIN', 'ETIMEDOUT'].includes(code);
    assert.deepEqual(f.exits, [blocked ? 31 : 1]);
    assert.deepEqual(f.out, blocked ? ['NETWORK_BLOCKED'] : []);
    assert.equal(f.result().error_code, blocked ? code : 'OTHER');
    assert.equal(f.calls.length, 1);
  });
}

for (const [label, status, body, code] of [
  ['malformed JSON', 200, '{"token":"' + PRIVATE, 'INVALID_JSON'],
  ['oversized body', 200, PRIVATE.repeat(1500), 'OVERSIZED'],
  ['wrong HTTP status', 500, '{"name":"is-number","version":"7.0.0"}', 'RESPONSE'],
  ['wrong package', 200, JSON.stringify({name: PRIVATE, version: '7.0.0'}), 'RESPONSE'],
  ['wrong version', 200, JSON.stringify({name: 'is-number', version: PRIVATE}), 'RESPONSE'],
  ['null response', 200, 'null', 'RESPONSE'],
]) {
  test(label + ' stays a failed qualification without content disclosure', () => {
    const f = fixture(); connected(f);
    const response = f.respond(status);
    response.emit('data', body); response.emit('end');
    assert.deepEqual(f.exits, [1]); assert.deepEqual(f.out, []);
    assert.equal(f.result().error_code, code);
    assert.equal(f.calls.length, 1);
  });
}

test('response errors and synchronous transport errors are redacted without success', () => {
  const f = fixture(); connected(f);
  const response = f.respond();
  response.emit('error', {code: PRIVATE, message: PRIVATE});
  assert.deepEqual(f.exits, [1]); assert.deepEqual(f.out, []); assert.equal(f.result().error_code, 'OTHER');
  const sync = fixture({code: PRIVATE, message: PRIVATE});
  assert.deepEqual(sync.exits, [1]); assert.deepEqual(sync.out, []); assert.equal(sync.result().error_code, 'OTHER');
});
