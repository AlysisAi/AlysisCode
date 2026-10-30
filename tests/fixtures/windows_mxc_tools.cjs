'use strict';
const fs = require('node:fs');
const cp = require('node:child_process');
const net = require('node:net');
const path = require('node:path');
const outside = process.argv[2];
let failures = 0;
function check(name, ok, extra = {}) { console.log(JSON.stringify({name, ok, ...extra})); if (!ok) failures++; }
function deny(name, action, sharingLock = false) {
  try { action(); check(name, false); }
  catch (e) { check(name, ['EACCES', 'EPERM', ...(sharingLock ? ['EBUSY'] : [])].includes(e.code), {code:e.code}); }
}
async function main() {
  fs.writeFileSync('inside.txt', 'inside-ok');
  check('workspace_rw', fs.readFileSync('inside.txt', 'utf8') === 'inside-ok');
  fs.mkdirSync('rename-directory');
  fs.writeFileSync('rename-source', 'replacement');
  fs.writeFileSync('rename-directory/target', 'old');
  fs.renameSync('rename-source', 'rename-directory/target');
  check('workspace_rename_replace', fs.readFileSync('rename-directory/target', 'utf8') === 'replacement'
    && !fs.existsSync('rename-source'));
  fs.renameSync('rename-directory', 'renamed-directory');
  check('workspace_directory_rename', fs.readFileSync('renamed-directory/target', 'utf8') === 'replacement');
  fs.unlinkSync('renamed-directory/target'); fs.rmdirSync('renamed-directory');
  check('workspace_file_directory_delete', !fs.existsSync('renamed-directory'));
  fs.writeFileSync('outside-rename-source', 'stay-inside');
  deny('outside_rename', () => fs.renameSync('outside-rename-source', path.join(outside, 'rename-injected')), true);
  check('outside_rename_source_preserved', fs.readFileSync('outside-rename-source', 'utf8') === 'stay-inside');
  deny('outside_read', () => fs.readFileSync(path.join(outside, 'canary')));
  deny('outside_write', () => fs.writeFileSync(path.join(outside, 'canary'), 'modified'));
  deny('git_write', () => fs.writeFileSync('.git/canary', 'modified'));
  deny('git_rename', () => fs.renameSync('.git', '.git-moved'), true);
  deny('git_delete', () => fs.rmSync('.git', {recursive:true}), true);
  deny('git_hardlink_creation', () => fs.linkSync('.git/canary', 'metadata-alias'));
  fs.writeFileSync('staged-file', 'staged');
  fs.mkdirSync('staged-dir');
  deny('git_file_rename_in', () => fs.renameSync('staged-file', '.git/injected-file'), true);
  deny('git_directory_rename_in', () => fs.renameSync('staged-dir', '.git/injected-dir'), true);
  deny('git_hardlink_in', () => fs.linkSync('inside.txt', '.git/injected-link'));
  const sync = cp.spawnSync(process.execPath, ['-e', 'process.stdout.write("SYNC_OK")'], {timeout:5000});
  check('spawnSync', sync.status === 0 && sync.stdout?.toString() === 'SYNC_OK', {error:sync.error?.code});
  await new Promise(resolve => {
    const child = cp.spawn(process.execPath, ['-e', 'process.stdout.write("ASYNC_OK")']);
    let text = ''; let settled = false;
    const timer = setTimeout(() => {child.kill(); finish(false);}, 5000);
    function finish(ok) {if (settled) return; settled=true; clearTimeout(timer); check('spawn',ok);resolve();}
    child.stdout.on('data', x => text += x);
    child.on('error', () => finish(false));
    child.on('close', code => finish(code === 0 && text === 'ASYNC_OK'));
  });
  fs.writeFileSync('ipc-child.cjs', 'process.send("IPC_OK"); process.disconnect();');
  await new Promise(resolve => {
    const child = cp.fork('./ipc-child.cjs', [], {silent:true});
    let received=false;let settled=false;
    const timer=setTimeout(() => {child.kill();finish(false);},5000);
    function finish(ok) {if(settled)return;settled=true;clearTimeout(timer);check('fork_ipc',ok);resolve();}
    child.on('message', message => received = message === 'IPC_OK');
    child.on('error', () => finish(false));
    child.on('close', code => finish(code === 0 && received));
  });
  await new Promise(resolve => {
    const socket=net.connect({host:'1.1.1.1',port:443}); let settled=false;
    function finish(ok,extra) {if(settled)return;settled=true;socket.destroy();check('offline_numeric_tcp',ok,extra);resolve();}
    socket.setTimeout(2500,()=>finish(false,{timeout:true}));
    socket.on('connect',()=>finish(false,{connected:true}));
    socket.on('error',e=>finish(['EACCES','EPERM'].includes(e.code),{code:e.code}));
  });
  process.exitCode=failures?1:0;
}
main().catch(error=>{console.error(error.code||error.message);process.exitCode=2;});
