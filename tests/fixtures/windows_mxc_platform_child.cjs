'use strict';

// Finite broker target: no shells, descendants, network, or persistent changes.
const fs = require('node:fs');
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
if (!/^[0-9a-f]{64}$/.test(config.nonce)) throw new Error('invalid synthetic nonce');
const result = {nonce: config.nonce, outside_written: false};
try {
  fs.writeFileSync(config.marker, config.nonce, {flag: 'wx', encoding: 'ascii'});
  result.outside_written = true;
} catch (error) {
  result.error_code = error.code;
}
fs.writeFileSync(config.ack, JSON.stringify(result), {flag: 'wx', encoding: 'utf8'});
