'use strict';
const fs=require('node:fs');
const cp=require('node:child_process');
const path=require('node:path');
const [role, directory, nonce]=process.argv.slice(2);
const file=name=>path.join(directory,name);
fs.writeFileSync(file(role+'.json'),JSON.stringify({pid:process.pid,parent:process.ppid,nonce}));
if(role==='root')cp.spawn(process.execPath,[__filename,'child',directory,nonce],{stdio:'ignore',detached:true});
if(role==='child')cp.spawn(process.execPath,[__filename,'grandchild',directory,nonce],{stdio:'ignore',detached:true});
const interval=setInterval(()=>{
  fs.writeFileSync(file(role+'.heartbeat'),String(Date.now()));
  if(role==='root' && fs.existsSync(file('release-root')))process.exit(0);
},30);
// Independent upper bound even if a containment regression leaves an orphan.
setTimeout(()=>{clearInterval(interval);process.exit(0);},20000);
