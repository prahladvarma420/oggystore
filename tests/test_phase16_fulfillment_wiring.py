"""16.6: real HTTPS stub transport + real order/refund/delivery services.

Only DNS-to-test-IP/CA trust transport boundaries are redirected to loopback.
Production contains no localhost exception, insecure TLS or test transport.
"""
import contextlib
import datetime
import hashlib
import hmac
import io
import json
import logging
import os
import ssl
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import requests
import tests.test_phase16_manual_delivery as fixtures
from fulfillment.external_clients import generic_api_client as generic
from fulfillment.router import route_fulfillment
from reseller_api.services.order_failure import handle_post_debit_failure
from reseller_api.services.order_status import api_order_status
from reseller_api.services.reconciliation import reconcile_reseller
from utils.crypto import decrypt_text

db,rdb=fixtures.db,fixtures.rdb
SECRET='synthetic-api-credential-never-logged'
HMAC_SECRET='synthetic-hmac-credential-never-logged'

class Stub(BaseHTTPRequestHandler):
    calls=[]
    lock=threading.Lock()
    def log_message(self,*args):pass
    def do_POST(self):
        raw=self.rfile.read(int(self.headers.get('Content-Length',0)));payload=json.loads(raw)
        # Supplier can see the committed order through an independent connection.
        import sqlite3
        with sqlite3.connect(db.DB_PATH) as connection:
            oid=int(payload['order_id'].replace('ORD-',''))
            committed=connection.execute('SELECT id FROM orders WHERE id=?',(oid,)).fetchone() is not None
        call={'path':self.path,'headers':dict(self.headers),'body':raw,'payload':payload,'committed':committed}
        with self.lock:
            self.calls.append(call);attempt=sum(c['path']==self.path and c['payload']['order_id']==payload['order_id'] for c in self.calls)
        code=200;body={'status':'fulfilled','items':[f'SUPPLIER-CODE-{i} 🧩' for i in range(payload['quantity'])]}
        if self.path=='/terminal':code=400;body={'error':SECRET,'config':HMAC_SECRET}
        elif self.path=='/transient' and attempt==1:code=503
        elif self.path=='/exhausted':code=503
        elif self.path=='/malformed':body={'delivery':'','error':SECRET}
        elif self.path=='/mismatch':body={'items':['only-one']}
        elif self.path=='/pending':code=202;body={'status':'pending','reference':'opaque-upstream-reference'}
        elif self.path=='/supplier_place':code=202;body={'status':'pending','reference':f'SUP-{oid}'}
        elif self.path=='/redirect':code=302
        elif self.path=='/oversized':body={'delivery':'x'*70000}
        elif self.path=='/slow':time.sleep(4)
        elif self.path=='/credential_echo':body={'delivery':SECRET}
        encoded=json.dumps(body,ensure_ascii=False).encode()
        try:
            self.send_response(code);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(encoded)));self.end_headers();self.wfile.write(encoded)
        except (BrokenPipeError,ConnectionResetError,ssl.SSLError):pass

class Phase16FulfillmentWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.Phase16ManualDeliveryTests.setUpClass()
        cls.tmp=tempfile.TemporaryDirectory(prefix='fulfillment_tls_')
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'supplier.example')])
        now=datetime.datetime.now(datetime.timezone.utc)
        cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=5))
            .not_valid_after(now+datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('supplier.example')]),critical=False)
            .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256()))
        cls.ca=str(Path(cls.tmp.name)/'test-ca.pem');kp=Path(cls.tmp.name)/'key.pem'
        Path(cls.ca).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        kp.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.TraditionalOpenSSL,serialization.NoEncryption()))
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Stub);cls.server.daemon_threads=True
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(cls.ca,str(kp))
        cls.server.socket=ctx.wrap_socket(cls.server.socket,server_side=True)
        cls.port=cls.server.server_address[1]
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=3)
        cls.tmp.cleanup();fixtures.Phase16ManualDeliveryTests.tearDownClass()
    def setUp(self):
        self.fx=fixtures.Phase16ManualDeliveryTests('test_pending_to_delivered_real_endpoint_ciphertext_and_event')
        self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.api=self.fx.api;self.owner=self.fx.fx.owner
        def pool(adapter):
            result = adapter.poolmanager.connection_from_host('127.0.0.1',self.port,scheme='https',
                pool_kwargs={'assert_hostname':'supplier.example','server_hostname':'supplier.example'})
            result.ConnectionCls = generic.QuietHTTPSConnection
            return result
        def session():
            s=requests.Session();s.trust_env=False;s.verify=self.ca;return s
        self.transport=contextlib.ExitStack();self.addCleanup(self.transport.close)
        self.transport.enter_context(patch.object(generic,'resolve_public_ips',return_value=('93.184.216.34',)))
        self.transport.enter_context(patch.object(generic.PinnedHTTPSAdapter,'_pool',pool))
        self.transport.enter_context(patch.object(generic,'new_session',side_effect=session))
    def product(self,path='/fulfilled',quantity=1,ftype='external_api',**cfgextra):
        support=fixtures.fixtures.support;n=support._n()
        rs=rdb.create_reseller(f'P16F{n}',9690000+n,f'p16f{n}')
        rdb.set_reseller_status(rs['id'],'active');key,secret=rdb.create_api_key(rs['id'],'test')
        cfg={'endpoint':f'https://supplier.example:{self.port}{path}','api_key':SECRET,
             'hmac_secret':HMAC_SECRET,'retries':2,'dispatch_budget':5,**cfgextra}
        db.cur.execute('INSERT INTO products(name,price,stock,delivery,active,max_per_order,fulfillment_type,fulfillment_config) VALUES(?,10,10,?,1,5,?,?)',
            (f'Fulfillment {n}','' if ftype!='manual' else 'EXISTING-MANUAL-TEXT',ftype,json.dumps(cfg)))
        pid=db.cur.lastrowid;db.conn.commit()
        rdb.set_product_access(rs['id'],pid,True);rdb.import_reseller_product(rs['id'],pid)
        rdb.post_wallet_transaction(rs['id'],'topup_credit',50000,f'fulfillment-seed-{n}','seed')
        return rs,pid,rdb.ensure_product_public_id(pid),support._hdr(key,secret),f'fulfillment-{n}',cfg
    def create(self,fixture,quantity=1):
        rs,pid,pub,h,coid,cfg=fixture
        r=self.api.post('/api/v1/orders',headers=h,json={'product_id':pub,'quantity':quantity,'client_order_id':coid})
        self.assertEqual(r.status_code,201,r.get_json());data=r.get_json()['data']
        return int(data['order_id'].replace('ORD-','')),data
    def calls(self,oid):
        with Stub.lock:return [c for c in Stub.calls if c['payload']['order_id']==f'ORD-{oid:08d}']
    def scalar(self,sql,args=()):
        db.cur.execute(sql,args);return db.cur.fetchone()[0]
    def events(self,oid,event):
        return self.scalar('SELECT COUNT(*) FROM webhook_outbox WHERE aggregate_id=? AND event_type=?',(f'ORD-{oid:08d}',event))
    def assert_refund(self,rs,oid):
        self.assertEqual(api_order_status(oid),'failed')
        self.assertEqual(rdb.get_wallet_balance_cents(rs['id']),50000)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM reseller_wallet_transactions WHERE reference=?",(f'rapi.refund.order.{oid}',)),1)
        self.assertEqual(self.events(oid,'order.failed'),1)
        self.assertEqual(reconcile_reseller(rs['id']),[])
    def test_success_encrypted_real_delivery_and_committed_http(self):
        f=self.product();oid,data=self.create(f)
        self.assertEqual(data['fulfillment'],{'type':'external_api','status':'fulfilled'})
        self.assertEqual((data['status'],data['delivery_status']),('completed','delivered'))
        token=self.scalar('SELECT delivery FROM orders WHERE id=?',(oid,))
        self.assertTrue(token.startswith('enc:'));self.assertEqual(decrypt_text(token),'SUPPLIER-CODE-0 🧩')
        actual=self.fx.read(oid,f[3]);self.assertEqual(actual['items'],['SUPPLIER-CODE-0 🧩'])
        self.assertEqual(self.events(oid,'delivery.completed'),1);self.assertEqual(self.events(oid,'delivery.pending'),1)
        self.assertTrue(self.calls(oid)[0]['committed']);self.assertEqual(reconcile_reseller(f[0]['id']),[])
    def test_terminal_failure_full_refund(self):
        f=self.product('/terminal');oid,data=self.create(f)
        self.assertEqual(data['fulfillment']['status'],'terminal_error');self.assert_refund(f[0],oid)
        self.assertEqual(len(self.calls(oid)),1)
    def test_transient_then_success_hmac_and_stable_idempotency(self):
        f=self.product('/transient');oid,data=self.create(f)
        self.assertEqual(data['fulfillment']['status'],'fulfilled');calls=self.calls(oid);self.assertEqual(len(calls),2)
        self.assertEqual(calls[0]['headers']['Idempotency-Key'],calls[1]['headers']['Idempotency-Key'])
        for c in calls:
            stamp=c['headers']['X-Fulfillment-Timestamp']
            expected='sha256='+hmac.new(HMAC_SECRET.encode(),stamp.encode()+b'.'+c['body'],hashlib.sha256).hexdigest()
            self.assertEqual(c['headers']['X-Fulfillment-Signature'],expected)
    def test_exhausted_retry_limit_refunds_once(self):
        f=self.product('/exhausted',retries=3);oid,data=self.create(f)
        self.assertEqual(len(self.calls(oid)),4);self.assert_refund(f[0],oid)
        handle_post_debit_failure(oid);self.assert_refund(f[0],oid)
    def test_replay_no_second_supplier_request_or_new_response_object(self):
        f=self.product();oid,data=self.create(f)
        r=self.api.post('/api/v1/orders',headers=f[3],json={'product_id':f[2],'quantity':1,'client_order_id':f[4]})
        self.assertEqual(r.status_code,200,r.get_json());self.assertEqual(len(self.calls(oid)),1)
        self.assertNotIn('fulfillment',r.get_json()['data'])
    def test_malformed_200_refunds(self):
        f=self.product('/malformed');oid,data=self.create(f);self.assert_refund(f[0],oid)
    def test_quantity_two_validated_and_real_items(self):
        f=self.product();oid,data=self.create(f,2)
        self.assertEqual(self.calls(oid)[0]['payload']['quantity'],2)
        self.assertEqual(self.fx.read(oid,f[3])['items'],['SUPPLIER-CODE-0 🧩','SUPPLIER-CODE-1 🧩'])
    def test_quantity_mismatch_refunds_full_charge(self):
        f=self.product('/mismatch');oid,data=self.create(f,2);self.assert_refund(f[0],oid)
    def test_delivery_persistence_failure_is_pending_recoverable(self):
        f=self.product()
        with patch('reseller_api.services.order_idempotency.safe_write_delivery',side_effect=RuntimeError(SECRET)):
            oid,data=self.create(f)
        self.assertEqual(data['fulfillment']['status'],'pending');self.assertEqual(api_order_status(oid),'processing')
        self.assertIsNone(self.scalar('SELECT delivery FROM orders WHERE id=?',(oid,)))
        self.assertEqual(self.events(oid,'delivery.completed'),0);self.assertEqual(self.events(oid,'delivery.pending'),1)
        self.assertEqual(rdb.get_wallet_balance_cents(f[0]['id']),49000)
        self.assertEqual(reconcile_reseller(f[0]['id']),[])
    def test_completion_outbox_failure_rolls_back_to_pending(self):
        f=self.product()
        with patch('reseller_api.services.webhook_outbox.emit_event_atomic',side_effect=RuntimeError(SECRET)):
            oid,data=self.create(f)
        self.assertEqual(data['fulfillment']['status'],'pending');self.assertEqual(api_order_status(oid),'processing')
        self.assertIsNone(self.scalar('SELECT delivery FROM orders WHERE id=?',(oid,)))
    def test_pending_durable_no_refund_no_worker_started(self):
        f=self.product('/pending');oid,data=self.create(f)
        self.assertEqual(data['fulfillment']['status'],'pending');self.assertEqual(self.events(oid,'delivery.pending'),1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM reseller_wallet_transactions WHERE reference=?",(f'rapi.refund.order.{oid}',)),0)
        # 16.7 now adds the explicitly authorized nullable reference column.
        self.assertTrue(self.scalar('SELECT fulfillment_ref FROM orders WHERE id=?',(oid,)))
    def test_supplier_placement_pending_with_reference(self):
        f=self.product('/supplier_place',ftype='supplier_api',supplier_product_id='TEST-SUPPLIER-ID');oid,data=self.create(f)
        self.assertEqual(data['fulfillment'],{'type':'supplier_api','status':'pending'})
        self.assertEqual(len(self.calls(oid)),1)
        from fulfillment.references import decode_reference
        saved=decode_reference(self.scalar('SELECT fulfillment_ref FROM orders WHERE id=?',(oid,)))
        self.assertEqual(saved['value'],f'SUP-{oid}')
    def test_code_stock_unchanged_bytes_no_http(self):
        f=self.product(ftype='code_stock')
        db.cur.execute("INSERT INTO product_stock(product_id,delivery,sold) VALUES(?,'UNCHANGED-CODE',0)",(f[1],));db.conn.commit()
        oid,data=self.create(f)
        self.assertEqual(self.fx.read(oid,f[3])['items'],['UNCHANGED-CODE'])
        self.assertEqual(data['fulfillment'],{'type':'code_stock','status':'fulfilled'})
        self.assertEqual(self.calls(oid),[])
    def test_manual_static_unchanged_no_http(self):
        f=self.product(ftype='manual');oid,data=self.create(f)
        self.assertEqual(decrypt_text(self.scalar('SELECT delivery FROM orders WHERE id=?',(oid,))),'EXISTING-MANUAL-TEXT')
        self.assertEqual(data['fulfillment'],{'type':'manual','status':'fulfilled'});self.assertEqual(self.calls(oid),[])
    def test_adversarial_logs_api_outbox_no_secrets(self):
        stream=io.StringIO();handler=logging.StreamHandler(stream);root=logging.getLogger();old=root.level
        root.addHandler(handler);root.setLevel(logging.DEBUG)
        try:
            f=self.product('/terminal');oid,data=self.create(f)
            product_response=self.owner.get('/api/products').get_json()
        finally:root.removeHandler(handler);root.setLevel(old)
        serialized=json.dumps(data)+json.dumps(product_response)+stream.getvalue()
        db.cur.execute('SELECT payload_json FROM webhook_outbox WHERE aggregate_id=?',(f'ORD-{oid:08d}',))
        serialized+=''.join(r[0] for r in db.cur.fetchall())
        self.assertNotIn(SECRET,serialized);self.assertNotIn(HMAC_SECRET,serialized)
        self.assertNotIn('fulfillment_config',json.dumps(data));self.assertTrue(all('fulfillment_config' not in p for p in product_response['products']))
    def test_global_http_debug_cannot_print_supplier_credentials(self):
        import http.client
        f=self.product();stream=io.StringIO()
        with patch.object(http.client.HTTPConnection,'debuglevel',1), contextlib.redirect_stdout(stream):
            oid,data=self.create(f)
        self.assertEqual(data['fulfillment']['status'],'fulfilled')
        self.assertNotIn(SECRET,stream.getvalue());self.assertNotIn(HMAC_SECRET,stream.getvalue())

    def test_invalid_configuration_rejected_before_product_write(self):
        f=self.product();before=self.scalar('SELECT fulfillment_config FROM products WHERE id=?',(f[1],))
        for config in ('{bad-json','[]',json.dumps({'endpoint':'http://supplier.example'}),json.dumps({'endpoint':'https://127.0.0.1'})):
            r=self.owner.post(f'/api/products/{f[1]}/update',headers={'X-CSRF-Token':'test-csrf'},json={'fulfillment_type':'external_api','fulfillment_config':config,'price':99})
            self.assertEqual(r.status_code,400,r.get_json())
            self.assertEqual(self.scalar('SELECT fulfillment_config FROM products WHERE id=?',(f[1],)),before)
            self.assertEqual(self.scalar('SELECT price FROM products WHERE id=?',(f[1],)),10)
    def test_saved_config_encrypted_write_only_blank_preserves(self):
        f=self.product();r=self.owner.post(f'/api/products/{f[1]}/update',headers={'X-CSRF-Token':'test-csrf'},json={'fulfillment_type':'external_api','fulfillment_config':json.dumps(f[5])})
        self.assertEqual(r.status_code,200,r.get_json());stored=self.scalar('SELECT fulfillment_config FROM products WHERE id=?',(f[1],));self.assertTrue(stored.startswith('enc:'))
        self.assertNotIn(SECRET,json.dumps(r.get_json()))
        self.assertNotIn(SECRET,json.dumps(self.owner.get('/api/products').get_json()))
        r=self.owner.post(f'/api/products/{f[1]}/update',headers={'X-CSRF-Token':'test-csrf'},json={'fulfillment_type':'external_api','price':12})
        self.assertEqual(r.status_code,200,r.get_json())
        cfg=generic.load_config(self.scalar('SELECT fulfillment_config FROM products WHERE id=?',(f[1],)));self.assertEqual(cfg['api_key'],SECRET)
        oid,data=self.create(f);self.assertEqual(data['fulfillment']['status'],'fulfilled')
    def test_nonowner_cannot_change_fulfillment(self):
        f=self.product();admin=self.fx.fx.fx._owner('admin')
        r=admin.post(f'/api/products/{f[1]}/update',headers={'X-CSRF-Token':'test-csrf'},json={'fulfillment_type':'manual'})
        self.assertEqual(r.status_code,403)
    def test_private_dns_and_rebinding_no_request(self):
        f=self.product();oid,data=self.create(f)
        with patch.object(generic,'resolve_public_ips',side_effect=generic.FulfillmentConfigError()):
            result=route_fulfillment(oid,f[1],1)
        self.assertEqual(result['result']['category'],'ssrf_blocked');self.assertEqual(len(self.calls(oid)),1)
        # Exercise the real resolver, not the test stub redirection.
        self.transport.close()
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(generic.FulfillmentConfigError):generic.resolve_public_ips('https://supplier.example/')
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('93.184.216.34',443)),(10,1,6,'',('::1',443,0,0))]):
            with self.assertRaises(generic.FulfillmentConfigError):generic.resolve_public_ips('https://supplier.example/')
    def test_dns_changes_after_save_validation_before_connect(self):
        self.transport.close()
        public=[(2,1,6,'',('93.184.216.34',443))]
        private=[(2,1,6,'',('10.0.0.1',443))]
        cfg={'endpoint':'https://supplier.example/','retries':0}
        with patch('socket.getaddrinfo',side_effect=[public,private]):
            generic.validate_config(cfg)  # save-time policy accepts public DNS
            result=generic.GenericApiClient(cfg).fulfill(1,cfg,1)
        self.assertEqual(result,{'status':'terminal_error','category':'ssrf_blocked'})
    def test_router_entry_is_outside_order_atomic(self):
        f=self.product();states=[]
        def route(*args):
            states.append(db.get_conn().in_transaction)
            return route_fulfillment(*args)
        with patch('fulfillment.order_dispatch.route_fulfillment',side_effect=route):
            oid,data=self.create(f)
        self.assertEqual(states,[False]);self.assertTrue(self.calls(oid)[0]['committed'])
    def test_config_missing_encryption_save_fails_without_plaintext(self):
        f=self.product();before=self.scalar('SELECT fulfillment_config FROM products WHERE id=?',(f[1],))
        with patch.dict(os.environ,{'DATA_ENCRYPTION_KEY':''}):
            response=self.owner.post(f'/api/products/{f[1]}/update',headers={'X-CSRF-Token':'test-csrf'},
                json={'fulfillment_type':'external_api','fulfillment_config':f[5]})
        self.assertEqual(response.status_code,503);self.assertEqual(self.scalar('SELECT fulfillment_config FROM products WHERE id=?',(f[1],)),before)
    def test_secret_echo_is_not_delivery(self):
        f=self.product('/credential_echo');oid,data=self.create(f);self.assert_refund(f[0],oid)
        self.assertIsNone(self.scalar('SELECT delivery FROM orders WHERE id=?',(oid,)))

    def test_pinned_adapter_retains_host_sni_certificate_validation(self):
        self.transport.close();adapter=generic.PinnedHTTPSAdapter('supplier.example','93.184.216.34',443)
        pool=adapter.get_connection('https://supplier.example/');self.assertEqual(pool.host,'93.184.216.34')
        self.assertEqual(pool.assert_hostname,'supplier.example');self.assertEqual(pool.conn_kw['server_hostname'],'supplier.example')
        adapter.close()
    def test_dispatch_dns_stall_bounded_no_late_delivery_write(self):
        f=self.product(dispatch_budget=3)
        def slow(endpoint):time.sleep(4);return ('93.184.216.34',)
        with patch.object(generic,'resolve_public_ips',side_effect=slow):
            start=time.monotonic();oid,data=self.create(f);elapsed=time.monotonic()-start
            self.assertLess(elapsed,3.8);self.assertEqual(data['fulfillment']['status'],'pending')
            time.sleep(1.2)
        self.assertEqual(self.calls(oid),[]);self.assertEqual(api_order_status(oid),'processing')
    def test_timeout_budget_returns_pending_not_speculative_refund(self):
        f=self.product('/slow',dispatch_budget=3,connect_timeout=1,read_timeout=1)
        start=time.monotonic();oid,data=self.create(f);self.assertLess(time.monotonic()-start,3.8)
        self.assertEqual(data['fulfillment']['status'],'pending')
        self.assertEqual(api_order_status(oid),'processing')
        self.assertEqual(rdb.get_wallet_balance_cents(f[0]['id']),49000)
    def test_exhausted_timeout_refunds_actual_http_attempt(self):
        f=self.product('/slow',dispatch_budget=5,connect_timeout=1,read_timeout=1,retries=0)
        oid,data=self.create(f)
        self.assertEqual(data['fulfillment']['status'],'terminal_error');self.assert_refund(f[0],oid)
        self.assertEqual(len(self.calls(oid)),1)
    def test_thread_start_failure_pending_and_slot_released(self):
        f=self.product('/pending');oid,data=self.create(f)
        from fulfillment.router import _DISPATCH_SLOTS
        with patch('fulfillment.router.threading.Thread.start',side_effect=RuntimeError(SECRET)):
            result=route_fulfillment(oid,f[1],1)
        self.assertEqual(result['result']['status'],'pending')
        acquired=0
        try:
            while _DISPATCH_SLOTS.acquire(blocking=False):acquired+=1
            self.assertEqual(acquired,16)
        finally:
            for _ in range(acquired):_DISPATCH_SLOTS.release()
    def test_refund_crash_reports_terminal_and_existing_engine_resumes(self):
        f=self.product('/terminal')
        with patch('reseller_api.services.order_failure._post_refund_serialized',side_effect=RuntimeError(SECRET)):
            oid,data=self.create(f)
        self.assertEqual(data['status'],'failed');self.assertEqual(data['fulfillment']['status'],'terminal_error')
        self.assertEqual(rdb.get_wallet_balance_cents(f[0]['id']),49000)
        self.assertEqual(handle_post_debit_failure(oid)['result'],'recovered')
        self.assert_refund(f[0],oid);self.assertEqual(len(self.calls(oid)),1)
    def test_adversarial_transport_exception_is_sanitized(self):
        f=self.product();oid,data=self.create(f);stream=io.StringIO()
        logger=logging.getLogger('urllib3.connectionpool');old=logger.level
        handler=logging.StreamHandler(stream);logger.addHandler(handler);logger.setLevel(logging.DEBUG)
        def bad_session():
            try:raise RuntimeError(SECRET+' '+HMAC_SECRET)
            except RuntimeError:
                logger.error('config=%s api_key=%s hmac=%s',json.dumps(f[5]),SECRET,HMAC_SECRET,exc_info=True)
            raise requests.exceptions.ConnectionError(SECRET)
        try:
            with patch.object(generic,'new_session',side_effect=bad_session):result=route_fulfillment(oid,f[1],1)
        finally:logger.removeHandler(handler);logger.setLevel(old)
        text=stream.getvalue()+json.dumps(result)
        self.assertNotIn(SECRET,text);self.assertNotIn(HMAC_SECRET,text)
        self.assertNotIn('Traceback',text);self.assertEqual(result['result']['category'],'connection')
    def test_owner_claim_conflict_leaves_no_duplicate_effects(self):
        f=self.product('/pending');oid,data=self.create(f)
        from reseller_api.services.order_idempotency import claim_worker_job
        from fulfillment.order_dispatch import dispatch_order
        claim=f'admin-order-lifecycle:{oid}'
        self.assertTrue(claim_worker_job(claim,'synthetic-owner'))
        try:
            db.cur.execute('UPDATE products SET fulfillment_config=? WHERE id=?',(json.dumps({**f[5],'endpoint':f[5]['endpoint'].replace('/pending','/terminal')}),f[1]));db.conn.commit()
            result=dispatch_order(oid,f[1],1)
            self.assertEqual(result['status'],'pending');self.assertEqual(api_order_status(oid),'processing')
            self.assertEqual(rdb.get_wallet_balance_cents(f[0]['id']),49000)
        finally:
            db.cur.execute('DELETE FROM worker_jobs WHERE job_key=?',(claim,));db.conn.commit()

    def test_concurrent_failure_engine_exactly_one_refund(self):
        f=self.product('/pending');oid,data=self.create(f);errors=[]
        def fail():
            try:handle_post_debit_failure(oid,reason='fulfillment_error: 5xx')
            except Exception as e:errors.append(type(e).__name__)
            finally:db.close_thread_connection()
        threads=[threading.Thread(target=fail) for _ in range(4)]
        for t in threads:t.start()
        for t in threads:t.join(10)
        self.assertFalse(any(t.is_alive() for t in threads));self.assertEqual(errors,[]);self.assert_refund(f[0],oid)
    def test_config_timeout_retry_budget_bounds(self):
        base={'endpoint':'https://supplier.example/'}
        for field,value in [('retries',4),('retries',-1),('connect_timeout',0),('read_timeout',61),('dispatch_budget',11),('dispatch_budget',0),('endpoint','https://user:pass@supplier.example'),('endpoint','https://supplier.example/?api_key=secret')]:
            with self.assertRaises(generic.FulfillmentConfigError):generic.validate_config({**base,field:value})
    def test_redirect_never_followed_and_refunded(self):
        f=self.product('/redirect');oid,data=self.create(f);self.assertEqual(len(self.calls(oid)),1);self.assert_refund(f[0],oid)
    def test_oversized_response_bounded_failure(self):
        f=self.product('/oversized');oid,data=self.create(f);self.assert_refund(f[0],oid)
    def test_tls_error_terminal_sanitized_no_retry(self):
        f=self.product();oid,data=self.create(f)
        def untrusted_session():s=requests.Session();s.trust_env=False;return s
        with patch.object(generic,'new_session',side_effect=untrusted_session):result=route_fulfillment(oid,f[1],1)
        self.assertEqual(result['result'],{'status':'terminal_error','category':'tls'})

if __name__=='__main__':unittest.main()
