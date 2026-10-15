"""Real Flask/Express HTTP round trips signed by the application's own signer.

No public receiver, live API credentials or application database. Node absence
is an explicit skip; missing Express with Node installed is an actual failure.
"""
import ast
from concurrent.futures import ThreadPoolExecutor
import contextlib
import copy
import hashlib
import http.client
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import yaml
from werkzeug.serving import make_server
from reseller_api.services import webhook_signature as signer

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
receiver=load('reference_receiver',ROOT/'examples/webhook_receiver.py')
generator=load('client_kit_generator',ROOT/'tools/generate_client_kit.py')
SECRET='test-only-webhook-signing-key-not-production'
NOW=1700000000


class Backend:
    def __init__(self,kind,path,automatic=False):
        self.kind=kind;self.path=path;self.now=NOW;self.calls=0;self.failed=False
        self.gate=threading.Event();self.started=threading.Event();self.logs=io.StringIO()
        if kind=='python':
            self.handler=logging.StreamHandler(self.logs);receiver.log.addHandler(self.handler);receiver.log.setLevel(logging.INFO)
            def processor(body):
                self.calls+=1;self.started.set()
                if automatic:self.gate.wait(timeout=5)
                if self.failed:raise RuntimeError('test-only-sensitive-processor-error')
                receiver.process_event(body)
            self.app=receiver.create_app(SECRET,path,processor=processor,clock=lambda:self.now,start_worker=automatic)
            self.controls=self.app.extensions['receiver']
            self.server=make_server('127.0.0.1',0,self.app,threaded=True)
            self.port=self.server.server_port
            self.thread=threading.Thread(target=lambda:self.server.serve_forever(poll_interval=0.01),daemon=True);self.thread.start()
        else:
            self.stderr=tempfile.TemporaryFile(mode='w+t')
            env=dict(os.environ,WEBHOOK_SECRET=SECRET,INBOX_DB=str(path),TEST_NOW=str(NOW),TEST_AUTOMATIC='1' if automatic else '0')
            self.process=subprocess.Popen([shutil.which('node'),str(ROOT/'tests/fixtures/webhook_receiver_harness.js')],
                cwd=ROOT,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.stderr,text=True,bufsize=1)
            self.port=self.read()['port']
    def read(self):
        ready,_,_=select.select([self.process.stdout],[],[],8)
        if not ready:raise AssertionError('Node harness output timed out')
        line=self.process.stdout.readline()
        if not line:
            self.stderr.seek(0);raise AssertionError('Node harness failed: '+self.stderr.read()[-2000:])
        return json.loads(line)
    def command(self,action,value=None):
        if self.kind=='python':
            if action=='process':return self.controls['process_one']()
            if action=='time':self.now=value;return True
            if action=='fail':self.failed=value;return True
            if action=='retry':return self.controls['store'].retry_failed()
            if action=='calls':return self.calls
            if action=='release':self.gate.set();return True
        self.process.stdin.write(json.dumps({'action':action,'value':value})+'\n');self.process.stdin.flush()
        return self.read()['result']
    def post(self,raw,headers):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=4)
        try:
            conn.request('POST','/webhook',body=raw,headers={'Content-Type':'application/json',**headers})
            res=conn.getresponse();return res.status,json.loads(res.read())
        finally:conn.close()
    def output(self):
        if self.kind=='python':return self.logs.getvalue()
        self.stderr.flush();self.stderr.seek(0);return self.stderr.read()
    def close(self):
        if self.kind=='python':
            self.gate.set();self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
            self.controls['close']();receiver.log.removeHandler(self.handler)
        elif self.process.poll() is None:
            try:self.command('close');self.process.wait(timeout=3)
            finally:
                if self.process.poll() is None:self.process.kill();self.process.wait()
                self.process.stdin.close();self.process.stdout.close();self.stderr.close()


def node_available():
    node=shutil.which('node')
    if not node:return False
    return int(subprocess.check_output([node,'-p','process.versions.node.split(".")[0]'],text=True).strip())>=24


class RoundTripMixin:
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='receiver-test-');self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'inbox.sqlite3';self.now=NOW
        self.backend=Backend(self.kind,self.path);self.addCleanup(lambda:self.backend.close())
    def payload(self,**values):
        return {'event_id':42,'event':'order.created','event_type':'order.created','delivery_id':'attempt-one','idempotency_key':'42',**values}
    def raw(self,body):return json.dumps(body,ensure_ascii=False,indent=2).encode('utf-8')
    def headers(self,raw,ts=None,event='order.created'):
        ts=str(self.now if ts is None else ts)
        return {signer.HEADER_SIGNATURE:signer.sign_body(SECRET,raw,ts),signer.HEADER_TIMESTAMP:ts,
                signer.HEADER_EVENT:event,signer.HEADER_DELIVERY:'attempt-header'}
    def send(self,body=None,**kwargs):
        raw=self.raw(self.payload() if body is None else body)
        return self.backend.post(raw,self.headers(raw,**kwargs))
    def rows(self):
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory=sqlite3.Row;return [dict(r) for r in conn.execute('SELECT * FROM inbox')]
    def test_valid_raw_bytes_unicode_html(self):
        body=self.payload(message='<b>héllo & नमस्ते 🙂</b>')
        raw=self.raw(body);status,result=self.backend.post(raw,self.headers(raw))
        self.assertEqual((status,result['duplicate']),(200,False));self.assertEqual(json.loads(self.rows()[0]['payload']),body)
        self.assertNotIn(body['message'],json.dumps(result))
    def test_integral_json_number_forms_dedupe_consistently(self):
        raw=self.raw(self.payload())
        for encoded in (b'42', b'42.0', b'4.2e1'):
            variant=raw.replace(b'"event_id": 42', b'"event_id": '+encoded)
            status,result=self.backend.post(variant,self.headers(variant))
            self.assertEqual(status,200)
            self.assertEqual(result['duplicate'],encoded!=b'42')
        self.assertEqual(len(self.rows()),1)

    def test_duplicate_changed_attempt_id_processed_once(self):
        self.assertEqual(self.send()[0],200)
        self.assertEqual(self.send(self.payload(delivery_id='attempt-two')),(200,{'ok':True,'duplicate':True}))
        self.assertTrue(self.backend.command('process'));self.assertFalse(self.backend.command('process'))
        self.assertEqual(self.backend.command('calls'),1);self.assertEqual(len(self.rows()),1)
    def test_tampered_and_reencoded_bytes_rejected(self):
        raw=self.raw(self.payload());headers=self.headers(raw)
        for bad in (raw+b' ',raw.replace(b'order.created',b'order.failed')):
            with self.subTest(raw=bad[:10]):self.assertEqual(self.backend.post(bad,headers)[0],401)
        self.assertEqual(self.rows(),[])
    def test_old_and_future_timestamps_rejected(self):
        raw=self.raw(self.payload())
        for ts in (NOW-301,NOW+301,0):
            with self.subTest(timestamp=ts):self.assertEqual(self.backend.post(raw,self.headers(raw,ts))[0],401)
        self.assertEqual(self.rows(),[])
    def test_timestamp_window_boundary_matches_signer(self):
        raw=self.raw(self.payload())
        for ts in (NOW-300,NOW+300):
            with patch.object(signer.time,'time',return_value=NOW):self.assertTrue(signer.verify_signature(SECRET,raw,str(ts),self.headers(raw,ts)[signer.HEADER_SIGNATURE]))
            self.assertEqual(self.backend.post(raw,self.headers(raw,ts))[0],200)
    def test_missing_malformed_incorrect_signatures(self):
        raw=self.raw(self.payload());original=self.headers(raw)
        variants=[{k:v for k,v in original.items() if k!=signer.HEADER_SIGNATURE},
            {**original,signer.HEADER_SIGNATURE:'sha256=bad'},
            {**original,signer.HEADER_SIGNATURE:'sha256='+'0'*64},
            {**original,signer.HEADER_TIMESTAMP:'not-a-timestamp'},
            {k:v for k,v in original.items() if k!=signer.HEADER_TIMESTAMP}]
        for headers in variants:self.assertEqual(self.backend.post(raw,headers)[0],401)
        self.assertEqual(self.rows(),[])
    def test_signature_and_timestamp_both_bound(self):
        raw=self.raw(self.payload());headers=self.headers(raw);headers[signer.HEADER_TIMESTAMP]=str(NOW+1)
        self.assertEqual(self.backend.post(raw,headers)[0],401)
    def test_signed_malformed_or_unsupported_payload_rejected(self):
        bodies=[b'not-json',b'[]',b'{}',b'{"event_id":42,"event_type":"order.created","number":1e999}',b'{"event_id":true,"event":"order.created"}',self.raw(self.payload(event_type='future.unsupported')),
                self.raw(self.payload(event_id=9007199254740992)),self.raw(self.payload(event_id=10**400)),self.raw(self.payload(event_id=42.1)),b'\xff',self.raw(self.payload(event_id='bad email@example.invalid'))]
        for raw in bodies:
            with self.subTest(raw=raw[:20]):self.assertEqual(self.backend.post(raw,self.headers(raw))[0],400)
        self.assertEqual(self.rows(),[])
    def test_event_header_and_alias_mismatch_rejected(self):
        raw=self.raw(self.payload())
        self.assertEqual(self.backend.post(raw,self.headers(raw,event='order.failed'))[0],400)
        self.assertEqual(self.send(self.payload(event='order.failed'))[0],400)
    def test_test_ping_and_dedup(self):
        body={'event':'test.ping','event_type':'test.ping','event_id':'ping-unit-123','idempotency_key':'ping-unit-123','message':'Webhook connectivity test','webhook_id':'WHK-00000001'}
        self.assertEqual(self.send(body,event='test.ping')[0],200)
        self.assertTrue(self.send(body,event='test.ping')[1]['duplicate'])
        self.assertTrue(self.backend.command('process'));self.assertIn('test.ping processed',self.backend.output())
    def test_duplicate_survives_restart(self):
        self.assertEqual(self.send()[0],200);self.backend.close();self.backend=Backend(self.kind,self.path)
        self.assertTrue(self.send()[1]['duplicate']);self.assertEqual(len(self.rows()),1)
        self.assertTrue(self.backend.command('process'));self.assertEqual(self.rows()[0]['status'],'done')
    def test_concurrent_duplicate_requests(self):
        raw=self.raw(self.payload());headers=self.headers(raw)
        with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(lambda _:self.backend.post(raw,headers),range(12)))
        self.assertEqual([s for s,b in results],[200]*12)
        self.assertEqual(sum(not b['duplicate'] for s,b in results),1)
        self.assertEqual(len(self.rows()),1)
    def test_post_ack_processing_failure_retries_locally(self):
        self.assertEqual(self.send()[0],200);self.backend.command('fail',True)
        self.assertTrue(self.backend.command('process'));row=self.rows()[0]
        self.assertEqual((row['status'],row['last_error']),('queued','processing_failed'))
        self.assertFalse(self.backend.command('process'));self.backend.command('time',NOW+3);self.backend.command('fail',False)
        self.assertTrue(self.backend.command('process'));self.assertEqual(self.rows()[0]['status'],'done')
        self.assertNotIn('sensitive-processor-error',self.backend.output())
    def test_failed_job_persists_in_dlq_and_manual_retry(self):
        self.send();self.backend.command('fail',True)
        for i in range(5):self.backend.command('time',NOW+61*i);self.assertTrue(self.backend.command('process'))
        self.assertEqual(self.rows()[0]['status'],'failed');self.assertFalse(self.backend.command('process'))
        self.assertEqual(self.backend.command('retry'),1);self.backend.command('fail',False)
        self.assertTrue(self.backend.command('process'));self.assertEqual(self.rows()[0]['status'],'done')
    def test_expired_processing_lease_recovers_after_restart(self):
        self.send()
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            conn.execute("UPDATE inbox SET status='processing',lease_token='crashed',lease_until=?,attempts=1",(NOW+60,));conn.commit()
        self.backend.close();self.backend=Backend(self.kind,self.path)
        self.assertFalse(self.backend.command('process'));self.backend.command('time',NOW+61)
        self.assertTrue(self.backend.command('process'));self.assertEqual(self.rows()[0]['status'],'done')
    def test_storage_failure_is_not_acknowledged(self):
        # A corrupt table is a real persistent-store failure, not a fake HTTP result.
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            conn.execute('DROP TABLE inbox');conn.commit()
        self.assertEqual(self.send()[0],503)

    def test_same_id_different_content_conflict(self):
        self.send();self.assertEqual(self.send(self.payload(quantity=2))[0],409);self.assertEqual(len(self.rows()),1)
    def test_payload_and_encoding_limits(self):
        raw=self.raw(self.payload());self.assertEqual(self.backend.post(raw,{**self.headers(raw),'Content-Encoding':'gzip'})[0],415)
        oversized=self.raw(self.payload(padding='x'*(256*1024)))
        self.assertEqual(self.backend.post(oversized,self.headers(oversized))[0],413);self.assertEqual(self.rows(),[])
    def test_no_secret_signature_or_payload_logging(self):
        body=self.payload(message='test-only-sensitive-message@example.invalid');raw=self.raw(body)
        self.backend.post(raw,self.headers(raw));self.backend.command('process')
        self.backend.post(raw,{**self.headers(raw),signer.HEADER_SIGNATURE:'sha256='+'0'*64})
        output=self.backend.output()
        for forbidden in (SECRET,body['message'],self.headers(raw)[signer.HEADER_SIGNATURE]):self.assertNotIn(forbidden,output)


class PythonRoundTrips(RoundTripMixin,unittest.TestCase):kind='python'
@unittest.skipUnless(node_available(),'Node.js 24+ unavailable: JavaScript receiver NOT EXECUTED')
class JavaScriptRoundTrips(RoundTripMixin,unittest.TestCase):kind='node'


class AsyncAckTests(unittest.TestCase):
    def check_ack(self,kind):
        with tempfile.TemporaryDirectory() as tmp:
            backend=Backend(kind,Path(tmp)/'inbox.sqlite3',automatic=True)
            try:
                raw=json.dumps({'event_id':8,'event_type':'test.ping'}).encode();ts=str(NOW)
                started=time.monotonic();status,result=backend.post(raw,{signer.HEADER_TIMESTAMP:ts,signer.HEADER_SIGNATURE:signer.sign_body(SECRET,raw,ts),signer.HEADER_EVENT:'test.ping'})
                self.assertEqual(status,200);self.assertLess(time.monotonic()-started,1)
                deadline=time.monotonic()+2
                while backend.command('calls')==0 and time.monotonic()<deadline:time.sleep(.02)
                self.assertEqual(backend.command('calls'),1)
                with contextlib.closing(sqlite3.connect(backend.path)) as conn:self.assertEqual(conn.execute('SELECT status FROM inbox').fetchone()[0],'processing')
                backend.command('release');deadline=time.monotonic()+2
                while time.monotonic()<deadline:
                    with contextlib.closing(sqlite3.connect(backend.path)) as conn:state=conn.execute('SELECT status FROM inbox').fetchone()[0]
                    if state=='done':break
                    time.sleep(.02)
                self.assertEqual(state,'done')
            finally:backend.close()
    def test_python_ack_does_not_wait_for_processing(self):self.check_ack('python')
    @unittest.skipUnless(node_available(),'Node.js 24+ unavailable: JavaScript async ACK NOT EXECUTED')
    def test_node_ack_does_not_wait_for_processing(self):self.check_ack('node')


class ClientKitTests(unittest.TestCase):
    def test_generated_files_are_current_and_deterministic(self):
        expected=generator.artifacts(ROOT)
        for name,text in expected.items():self.assertEqual((ROOT/name).read_text(),text,name)
        self.assertEqual(expected,generator.artifacts(ROOT))
    def test_postman_operation_and_body_coverage(self):
        spec=yaml.safe_load((ROOT/'docs/openapi.yaml').read_text());collection=json.loads((ROOT/'examples/OGGY-Reseller-API.postman_collection.json').read_text())
        actual={}
        for folder in collection['item']:
            for item in folder['item']:
                method,path=item['name'].split(' ',1);actual[(method.lower(),path)]=item['request']
        expected={(method,path) for path,item in spec['paths'].items() for method in item if method in generator.METHODS}
        self.assertEqual(set(actual),expected)
        for (method,path),req in actual.items():
            self.assertEqual(req['method'],method.upper());self.assertTrue(req['url']['raw'].startswith('{{base_url}}'))
            if 'requestBody' in spec['paths'][path][method]:self.assertIsInstance(json.loads(req['body']['raw']),(dict,list))
        self.assertEqual(collection['auth']['bearer'][0]['value'],'{{key_id}}.{{secret}}')
    def test_request_without_example_gets_schema_body(self):
        schema={'type':'object','required':['quantity','product_id'],
                'properties':{'quantity':{'type':'integer','minimum':2},'product_id':{'type':'string'}}}
        spec={'paths':{'/x':{'post':{'requestBody':{'content':{'application/json':{'schema':schema}}}}}}}
        artifact=generator.postman_artifacts(spec)
        collection=json.loads(artifact['examples/OGGY-Reseller-API.postman_collection.json']);req=collection['item'][0]['item'][0]['request']
        self.assertEqual(json.loads(req['body']['raw']),{'quantity':2,'product_id':'{{product_id}}'})
        self.assertIn('no explicit example',req['description'])
    def test_environments_have_no_hosts_or_credentials(self):
        for environment in ('test','production'):
            obj=json.loads((ROOT/f'examples/OGGY-{environment}.postman_environment.json').read_text())
            self.assertEqual({v['key']:v['value'] for v in obj['values']},{'base_url':'','key_id':'','secret':''})
        raw=(ROOT/'examples/OGGY-Reseller-API.postman_collection.json').read_text()
        self.assertNotIn('api.ggm25.shop',raw);self.assertNotIn('api-staging.example.invalid',raw)
        self.assertNotRegex(raw,r'rk_(?:test|live)_[A-Za-z0-9]')
    @unittest.skipUnless(node_available(),'Node.js unavailable: Postman schema validation NOT EXECUTED')
    def test_collection_validates_against_official_postman_schema(self):
        code="const fs=require('fs'),Ajv=require('ajv-draft-04'),formats=require('ajv-formats');const ajv=new Ajv({strict:false,allErrors:true});formats(ajv);const schema=JSON.parse(fs.readFileSync(process.argv[1]));const data=JSON.parse(fs.readFileSync(process.argv[2]));const validate=ajv.compile(schema);if(!validate(data)){console.error(JSON.stringify(validate.errors));process.exit(1);}console.log('schema valid');"
        result=subprocess.run([shutil.which('node'),'-e',code,str(ROOT/'examples/postman-collection-v2.1.schema.json'),str(ROOT/'examples/OGGY-Reseller-API.postman_collection.json')],cwd=ROOT/'examples',capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('schema valid',result.stdout)
    def test_vendored_official_schema_integrity(self):
        self.assertEqual(hashlib.sha256((ROOT/'examples/postman-collection-v2.1.schema.json').read_bytes()).hexdigest(),'90000a561d00b1a06ee37a6d3606e911e1357f757d8d91afc690d87dadacb9b2')

    def test_error_catalog_covers_all_current_error_producers(self):
        records=generator.error_inventory(ROOT)
        self.assertEqual(records,json.loads((ROOT/'examples/error_codes.json').read_text()))
        guide=(ROOT/'RESELLER_API.md').read_text();block=guide.split(generator.CATALOG_START,1)[1].split(generator.CATALOG_END,1)[0]
        codes=set(__import__('re').findall(r'^\| `([a-z0-9_]+)` \|',block,flags=__import__('re').M))
        self.assertEqual(codes,{x['code'] for x in records})
        for code in ('http_error','bad_request','authentication_failed','order_state_unavailable','buyer_email_required','daily_order_quota_exceeded','invalid_reference'):
            self.assertIn(code,codes)
    def test_new_literal_error_cannot_drift_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);shutil.copytree(ROOT/'reseller_api',root/'reseller_api',ignore=shutil.ignore_patterns('__pycache__'))
            p=root/'reseller_api/new_error.py';p.write_text('from reseller_api.errors import ApiError\ndef operation(): raise ApiError("new_client_kit_error", "New failure.", 418)\n')
            records=generator.error_inventory(root);self.assertIn('new_client_kit_error',{r['code'] for r in records})
            self.assertNotEqual(records,json.loads((ROOT/'examples/error_codes.json').read_text()))
    def test_new_dynamic_error_is_not_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);shutil.copytree(ROOT/'reseller_api',root/'reseller_api',ignore=shutil.ignore_patterns('__pycache__'))
            (root/'reseller_api/new_error.py').write_text('from reseller_api.errors import ApiError\ndef operation(code): raise ApiError(code, "New failure.", 400)\n')
            with self.assertRaisesRegex(ValueError,'Unresolved dynamic'):generator.error_inventory(root)
    def test_exact_header_names_and_event_documentation(self):
        for name in ('HEADER_SIGNATURE','HEADER_TIMESTAMP','HEADER_EVENT','HEADER_DELIVERY','FRESHNESS_WINDOW_SECONDS'):
            self.assertEqual(getattr(receiver,name),getattr(signer,name))
        spec=yaml.safe_load((ROOT/'docs/openapi.yaml').read_text());schemas=spec['components']['schemas']
        self.assertIn('test.ping',schemas['EventType']['enum']);self.assertNotIn('test.ping',schemas['SubscriptionEventType']['enum'])
        self.assertEqual(receiver.EVENTS,set(schemas['EventType']['enum']))
        self.assertIn('hmac.compare_digest',(ROOT/'examples/webhook_receiver.py').read_text())
        self.assertIn('crypto.timingSafeEqual',(ROOT/'examples/webhook_receiver.js').read_text())
