'use strict';

// Test fixture only. Direct execution keeps the endpoint, TLS defaults and
// overall deadline fixed; dependency injection is used solely by offline tests.
const https = require('https');
const URL = 'https://registry.npmjs.org/is-number/7.0.0';
const TAG = 'ALYSIS_HTTPS_STAGE ';
const BLOCKED = new Set(['EACCES', 'EPERM', 'ENOTFOUND', 'EAI_AGAIN', 'ETIMEDOUT']);
const SAFE_ERRORS = new Set([...BLOCKED, 'OVERSIZED', 'RESPONSE', 'INVALID_JSON']);

function runProbe(dependencies = {}) {
  const get = dependencies.get || https.get.bind(https);
  const clock = dependencies.clock || (() => Number(process.hrtime.bigint()) / 1e6);
  const setTimer = dependencies.setTimer || setTimeout;
  const clearTimer = dependencies.clearTimer || clearTimeout;
  const stdout = dependencies.stdout || (line => console.log(line));
  const stderr = dependencies.stderr || (line => console.error(line));
  const exit = dependencies.exit || (code => process.exit(code));
  const started = clock();
  const elapsed = () => Math.round(Math.max(0, clock() - started) * 1000) / 1000;
  const stages = {dns_ms: null, connect_ms: null, tls_ms: null, http_ms: null, end_ms: null};
  let lastStage = 'start';
  let status = null;
  let finished = false;
  const mark = (name, stage) => {
    if (!finished && stages[name] === null) {
      stages[name] = elapsed();
      lastStage = stage;
    }
  };
  const finish = (code, errorCode = null) => {
    if (finished) return;
    finished = true;
    clearTimer(deadline);
    stderr(TAG + JSON.stringify({last_stage: lastStage, ...stages, elapsed_ms: elapsed(),
      http_status: status, exit_code: code, error_code: errorCode}));
    if (code === 0) stdout('PUBLIC_HTTPS_OK');
    else if (code === 31) stdout('NETWORK_BLOCKED');
    exit(code);
  };
  const fail = error => {
    // Never stringify unknown errors, messages, addresses, headers or bodies.
    const code = SAFE_ERRORS.has(error?.code) ? error.code : 'OTHER';
    finish(BLOCKED.has(code) ? 31 : 1, code);
  };
  // Preserve the qualification deadline; diagnostics never add a retry.
  const deadline = setTimer(() => fail({code: 'ETIMEDOUT'}), 25000);
  try {
    const request = get(URL, response => {
      if (finished) return;
      mark('http_ms', 'http');
      if (Number.isInteger(response.statusCode) && response.statusCode >= 100 && response.statusCode <= 599) {
        status = response.statusCode;
      }
      let body = '';
      response.on('error', fail);
      response.on('data', chunk => {
        if (finished) return;
        body += chunk;
        if (body.length > 65536) fail({code: 'OVERSIZED'});
      });
      response.on('end', () => {
        if (finished) return;
        mark('end_ms', 'end');
        let data;
        try { data = JSON.parse(body); }
        catch { fail({code: 'INVALID_JSON'}); return; }
        if (response.statusCode !== 200 || data?.name !== 'is-number' || data?.version !== '7.0.0') {
          fail({code: 'RESPONSE'});
          return;
        }
        finish(0);
      });
    });
    request.on('socket', socket => {
      socket.once('lookup', () => mark('dns_ms', 'dns'));
      socket.once('connect', () => mark('connect_ms', 'connect'));
      socket.once('secureConnect', () => mark('tls_ms', 'tls'));
    });
    request.on('error', fail);
  } catch (error) {
    fail(error);
  }
}

module.exports = {runProbe};
if (require.main === module) runProbe();
