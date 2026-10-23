'use strict';
// Exercise the packed CLI and both security replacements without external traffic.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

async function main() {
  const root = path.resolve(process.argv[2] || '/usr/local/lib/node_modules/npm');
  const npm = path.join(root, 'bin/npm-cli.js');
  const npx = path.join(root, 'bin/npx-cli.js');
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'alysis-npm-smoke-'));
  const env = { ...process.env, NPM_CONFIG_CACHE: path.join(tmp, 'cache'),
    NPM_CONFIG_USERCONFIG: path.join(tmp, 'empty.npmrc'),
    NPM_CONFIG_GLOBALCONFIG: path.join(tmp, 'empty-global.npmrc'),
    NPM_CONFIG_UPDATE_NOTIFIER: 'false', NPM_CONFIG_AUDIT: 'false', NPM_CONFIG_FUND: 'false' };
  const run = (script, args, cwd = tmp) => execFileSync(process.execPath, [script, ...args],
    { cwd, env, encoding: 'utf8', timeout: 30000 }).trim();
  assert.equal(run(npm, ['--version']), '11.20.0-alysis.1');
  assert.equal(run(npx, ['--version']), '11.20.0-alysis.1');
  const { expand } = require(path.join(root, 'node_modules/brace-expansion'));
  assert.deepEqual(expand('src/{cli,ide}/{a,b}.js'),
    ['src/cli/a.js', 'src/cli/b.js', 'src/ide/a.js', 'src/ide/b.js']);
  const { MockAgent, request } = require(path.join(root, 'node_modules/undici'));
  const agent = new MockAgent();
  agent.disableNetConnect();
  agent.get('https://npm-smoke.invalid').intercept({ path: '/metadata' })
    .reply(200, { ok: true }, { headers: { 'content-type': 'application/json' } });
  const response = await request('https://npm-smoke.invalid/metadata', { dispatcher: agent });
  assert.deepEqual(await response.body.json(), { ok: true });
  await agent.close();
  const dep = path.join(tmp, 'dep');
  const app = path.join(tmp, 'app');
  fs.mkdirSync(dep); fs.mkdirSync(app);
  fs.writeFileSync(path.join(dep, 'package.json'), JSON.stringify({
    name: 'alysis-npm-offline-fixture', version: '1.0.0', main: 'index.js', bin: { 'alysis-fixture': 'cli.js' },
  }));
  fs.writeFileSync(path.join(dep, 'index.js'), 'module.exports = 42;\n');
  fs.writeFileSync(path.join(dep, 'cli.js'), '#!/usr/bin/env node\nconsole.log("fixture-ok");\n');
  const packed = JSON.parse(run(npm, ['pack', '--offline', '--ignore-scripts', '--json'], dep))[0];
  fs.writeFileSync(path.join(app, 'package.json'), JSON.stringify({
    name: 'alysis-npm-offline-app', version: '1.0.0',
    dependencies: { 'alysis-npm-offline-fixture': `file:../dep/${packed.filename}` },
    scripts: { test: 'node -e "if(require(\'alysis-npm-offline-fixture\')!==42)process.exit(1)"' },
  }));
  run(npm, ['install', '--package-lock-only', '--offline', '--ignore-scripts'], app);
  run(npm, ['ci', '--offline', '--ignore-scripts'], app);
  run(npm, ['test', '--offline'], app);
  assert.equal(run(npm, ['exec', '--offline', '--', 'alysis-fixture'], app), 'fixture-ok');
  assert.equal(run(npx, ['--offline', 'alysis-fixture'], app), 'fixture-ok');
  run(npm, ['ls', '--all'], app);
  console.log('Packed npm/npx, brace expansion, mocked HTTP, local pack, locked offline install, scripts and bin execution: passed');
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
