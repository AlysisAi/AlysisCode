'use strict';
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const { readFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const path = require('node:path');
const test = require('node:test');
const localRequire = createRequire(path.resolve('package.json'));
const entry = localRequire.resolve(process.env.PICOMATCH_UNDER_TEST || 'picomatch');
const pm = require(entry);
const root = path.dirname(entry);

test('the installed package contains the reviewed parser fix and honest rebuild provenance', () => {
  const manifest = JSON.parse(readFileSync(path.join(root,'package.json'),'utf8'));
  assert.equal(manifest.version,'4.0.8-alysis.1');
  assert.equal(manifest.alysisSecurityRebuild.patch_commit,'c63a5bf4e61dbdb81bc41e97dc2616c30acce337');
  assert.equal(createHash('sha256').update(readFileSync(path.join(root,'lib/parse.js'))).digest('hex'),
    'ccbcc9bb1333c4e03324fef8c729ea20979ef4f7b99d8f75c941abfec04b1cfd');
});

test('ordinary repeated wildcard matches, suffixes, escaping and capture semantics survive', () => {
  for (const [pattern,yes,no] of [
    ['+(*)c',['abc','c'],['ab!']],['+(*a)',['a','aba','aaa'],['b','aaa!']],
    ['+(a*)',['a','aba'],['b']],['+(a*b)',['ab','abab','acbab'],['a','aba']],
    ['+(a*b*c)',['abc','abcabc','axbyc'],['ab','abc!']],['*(a*b)c',['c','abc','ababc'],['abb']],
    ['x+(*a)y',['xay','xabaay'],['xby']]
  ]) {
    for (const input of yes) assert(pm.isMatch(input,pattern),`${pattern}: ${input}`);
    for (const input of no) assert(!pm.isMatch(input,pattern),`${pattern}: ${input}`);
    assert(!/\)[+*]/.test(pm.makeRe(pattern).source),pattern);
  }
  assert(pm.isMatch('*a*a','+(\\*a)'));
  assert(pm.isMatch('foobar','+(foo|bar)'));
  assert.deepEqual(pm.makeRe('+(*a)',{capture:true}).exec('aba').slice(1),['aba','ab']);
});

test('hostile failing inputs finish within an isolated process deadline', () => {
  const script=`const p=require(${JSON.stringify(entry)}); for(const pattern of ['+(*)a','+(*a)','*(a*)b','+(a*b)c']) p.isMatch('a'.repeat(10000)+'!',pattern);`;
  execFileSync(process.execPath,['-e',script],{timeout:2000,stdio:'pipe'});
});
