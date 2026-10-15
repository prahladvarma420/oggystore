'use strict';
// Test-only stdin control seam: never mounted on the receiver's HTTP routes.
const {createReceiver,processEvent} = require('../../examples/webhook_receiver.js');
const readline = require('node:readline');
console.info=(...args)=>console.error(...args); // keep stdin RPC stdout structured
let now=Number(process.env.TEST_NOW), fail=false, release=null, calls=0;
const automatic=process.env.TEST_AUTOMATIC==='1';
const processor=async body=>{
  calls++;
  if (automatic) await new Promise(resolve=>{release=resolve;});
  if (fail) throw new Error('test-only-sensitive-processor-error');
  await processEvent(body);
};
const receiver=createReceiver({secret:process.env.WEBHOOK_SECRET,dbPath:process.env.INBOX_DB,
  clock:()=>now,processor,startWorker:automatic});
const server=receiver.app.listen(0,'127.0.0.1',()=>console.log(JSON.stringify({port:server.address().port})));
const lines=readline.createInterface({input:process.stdin});
let chain=Promise.resolve();
lines.on('line',line=>{chain=chain.then(async()=>{
 const command=JSON.parse(line);let result;
 if(command.action==='process') result=await receiver.processOne();
 else if(command.action==='time') {now=command.value;result=true;}
 else if(command.action==='fail') {fail=command.value;result=true;}
 else if(command.action==='retry') result=receiver.store.retryFailed();
 else if(command.action==='calls') result=calls;
 else if(command.action==='release') {if(release)release();result=true;}
 else if(command.action==='close') {
   if(release)release();await new Promise(resolve=>server.close(resolve));await receiver.close();
   console.log(JSON.stringify({result:true}));process.exit(0);
 } else throw new Error('unknown test command');
 console.log(JSON.stringify({result}));
}).catch(()=>{console.error('test harness failed');process.exit(1);});});
