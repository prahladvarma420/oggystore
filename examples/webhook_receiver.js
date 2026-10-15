'use strict';
// Node 24+ / Express. Durable SQLite inbox; see README.md before deployment.
const crypto = require('node:crypto');
const {DatabaseSync} = require('node:sqlite');
const {TextDecoder} = require('node:util');
const express = require('express');
const HEADER_SIGNATURE = 'X-Oggystore-Signature-256';
const HEADER_TIMESTAMP = 'X-Oggystore-Timestamp';
const HEADER_EVENT = 'X-Oggystore-Event';
const HEADER_DELIVERY = 'X-Oggystore-Delivery';
const FRESHNESS_WINDOW_SECONDS = 300;
const EVENTS = new Set(['order.created', 'order.failed', 'order.cancelled',
  'delivery.pending', 'delivery.completed', 'delivery.failed', 'refund.created',
  'product.updated', 'product.disabled', 'product.enabled', 'product.deleted',
  'stock.updated', 'test.ping']);

function verify(secret, raw, headers, now) {
  const ts = headers[HEADER_TIMESTAMP.toLowerCase()] || '';
  const sig = headers[HEADER_SIGNATURE.toLowerCase()] || '';
  if (typeof ts !== 'string' || !/^[0-9]{1,12}$/.test(ts) ||
      typeof sig !== 'string' || !/^sha256=[a-f0-9]{64}$/.test(sig)) return false;
  if (Math.abs(Math.floor(now) - Number(ts)) > FRESHNESS_WINDOW_SECONDS) return false;
  const expected = 'sha256=' + crypto.createHmac('sha256', Buffer.from(secret, 'utf8'))
    .update(Buffer.from(ts + '.', 'ascii')).update(raw).digest('hex');
  return crypto.timingSafeEqual(Buffer.from(expected, 'ascii'), Buffer.from(sig, 'ascii'));
}
function canonical(value) {
  if (typeof value === 'number' && !Number.isFinite(value)) throw new Error('invalid number');
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort()
    .map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  return JSON.stringify(value);
}
function parseEvent(raw, headers) {
  const body = JSON.parse(new TextDecoder('utf-8', {fatal:true, ignoreBOM:true}).decode(raw));
  if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('invalid event');
  const id = body.event_id;
  if (!(typeof id === 'number' && Number.isSafeInteger(id) && id > 0) &&
      !(typeof id === 'string' && /^[A-Za-z0-9._:-]{1,128}$/.test(id))) throw new Error('invalid event id');
  const type = body.event_type === undefined ? body.event : body.event_type;
  if (!EVENTS.has(type) || (body.event !== undefined && body.event !== type) ||
      headers[HEADER_EVENT.toLowerCase()] !== type) throw new Error('unsupported event');
  const stable = {...body}; delete stable.delivery_id;
  return {id:String(id), type, body, hash:crypto.createHash('sha256').update(canonical(stable)).digest('hex')};
}
class Inbox {
  constructor(path) {
    if (path === ':memory:') throw new Error('Use a persistent SQLite file');
    this.db = new DatabaseSync(path);
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=1000;
      CREATE TABLE IF NOT EXISTS inbox (
        event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, payload TEXT NOT NULL,
        content_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
        lease_token TEXT, lease_until REAL, last_error TEXT, processed_at REAL);
      CREATE INDEX IF NOT EXISTS inbox_due ON inbox(status,next_at);`);
  }
  atomic(fn) {
    this.db.exec('BEGIN IMMEDIATE');
    try {const result=fn();this.db.exec('COMMIT');return result;}
    catch (_) {this.db.exec('ROLLBACK');throw new Error('inbox operation failed');}
  }
  accept(event) {
    return this.atomic(() => {
      const old=this.db.prepare('SELECT content_hash FROM inbox WHERE event_id=?').get(event.id);
      if (old) return old.content_hash === event.hash ? 'duplicate' : 'conflict';
      this.db.prepare('INSERT INTO inbox(event_id,event_type,payload,content_hash) VALUES(?,?,?,?)')
        .run(event.id,event.type,JSON.stringify(event.body),event.hash);
      return 'accepted';
    });
  }
  claim(now, leaseSeconds=60) {
    return this.atomic(() => {
      const job=this.db.prepare(`SELECT * FROM inbox WHERE (status='queued' AND next_at<=?)
        OR (status='processing' AND lease_until<=?) ORDER BY next_at,event_id LIMIT 1`).get(now,now);
      if (!job) return null;
      job.lease_token=crypto.randomUUID();job.attempts++;
      this.db.prepare(`UPDATE inbox SET status='processing',attempts=attempts+1,
        lease_token=?,lease_until=? WHERE event_id=?`).run(job.lease_token,now+leaseSeconds,job.event_id);
      return job;
    });
  }
  finish(job,now,failed=false) {
    const status=failed ? (job.attempts>=5 ? 'failed' : 'queued') : 'done';
    this.db.prepare(`UPDATE inbox SET status=?,next_at=?,last_error=?,processed_at=?,
      lease_token=NULL,lease_until=NULL WHERE event_id=? AND lease_token=? AND status='processing'`)
      .run(status,failed ? now+Math.min(60,2**Math.min(job.attempts,6)) : 0,
        failed ? 'processing_failed' : null,failed ? null : now,job.event_id,job.lease_token);
  }
  retryFailed() {return this.db.prepare("UPDATE inbox SET status='queued',attempts=0,next_at=0,last_error=NULL WHERE status='failed'").run().changes;}
  close() {this.db.close();}
}
async function processEvent(body) {
  if ((body.event_type || body.event) === 'test.ping') console.info('test.ping processed');
  else console.info('business event processed type=%s', body.event_type || body.event);
  // Add idempotent ASYNC business work; never print payloads/secrets/signatures.
}
function createReceiver({secret, dbPath, processor=processEvent, clock=()=>Date.now()/1000, startWorker=true}) {
  if (typeof secret !== 'string' || secret.length<16) throw new Error('Configure a signing secret of at least 16 characters');
  const store=new Inbox(dbPath);
  const app=express();app.disable('x-powered-by');
  let stopped=false, active=null;
  async function processOne() {
    const job=store.claim(clock());if (!job) return false;
    try {await processor(JSON.parse(job.payload));}
    catch (_) {console.warn('event processing failed');store.finish(job,clock(),true);return true;}
    store.finish(job,clock());return true;
  }
  const timer=startWorker ? setInterval(() => {
    if (stopped || active) return;
    active=processOne().catch(()=>console.warn('inbox worker unavailable')).finally(()=>{active=null;});
  },100) : null;
  if (timer) timer.unref();
  app.post('/webhook', express.raw({type:()=>true,limit:256*1024,inflate:false}), (req,res) => {
    if ((req.headers['content-encoding'] || 'identity').toLowerCase() !== 'identity') return res.status(415).json({ok:false,error:'unsupported_encoding'});
    const raw=Buffer.isBuffer(req.body) ? req.body : Buffer.alloc(0);
    if (!verify(secret,raw,req.headers,clock())) return res.status(401).json({ok:false,error:'invalid_signature'});
    let event;
    try {event=parseEvent(raw,req.headers);} catch (_) {return res.status(400).json({ok:false,error:'invalid_event'});}
    let outcome;
    try {outcome=store.accept(event);} catch (_) {console.warn('inbox unavailable');return res.status(503).json({ok:false,error:'inbox_unavailable'});}
    if (outcome==='conflict') return res.status(409).json({ok:false,error:'event_conflict'});
    return res.status(200).json({ok:true,duplicate:outcome==='duplicate'});
  });
  app.use((err,req,res,next) => { // No raw parser error, signature, body or traceback.
    const code=err.status===413 ? 413 : err.status===415 ? 415 : 400;
    res.status(code).json({ok:false,error:code===413?'payload_too_large':code===415?'unsupported_encoding':'invalid_request'});
  });
  return {app,store,processOne,async close() {stopped=true;if(timer)clearInterval(timer);if(active)await active;store.close();}};
}
module.exports={createReceiver,Inbox,verify,parseEvent,processEvent,EVENTS,HEADER_SIGNATURE,HEADER_TIMESTAMP,HEADER_EVENT,HEADER_DELIVERY,FRESHNESS_WINDOW_SECONDS};
if (require.main===module) {
  const path=process.env.INBOX_DB || 'receiver-node.sqlite3';
  if (process.argv.includes('--retry-failed')) {
    const store=new Inbox(path);console.info('Requeued failed inbox rows:',store.retryFailed());store.close();
  } else {
    const receiver=createReceiver({secret:process.env.WEBHOOK_SECRET || '',dbPath:path});
    const server=receiver.app.listen(Number(process.env.PORT || 8080),'127.0.0.1',()=>console.info('Receiver listening on loopback'));
    process.on('SIGTERM',()=>{server.close(async()=>{await receiver.close();process.exit(0);});});
  }
}
