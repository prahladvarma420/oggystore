"""16.7 real TLS supplier placement/status + real wallet/delivery/worker logic."""
import datetime
import json
import ssl
import threading
import unittest
from unittest.mock import patch
import tests.test_phase16_fulfillment_wiring as f
from reseller_api.workers import fulfillment_worker as w
from fulfillment.references import decode_reference
from reseller_api.services.order_status import api_order_status
from reseller_api.services.order_failure import handle_post_debit_failure
from utils.crypto import decrypt_text

class SupplierStub(f.Stub):
    states={}
    def do_POST(self):
        raw=self.rfile.read(int(self.headers.get('Content-Length',0)));p=json.loads(raw)
        oid=int(p['order_id'].replace('ORD-',''));poll=p.get('action')=='status'
        with self.lock:
            self.calls.append({'path':self.path,'payload':p,'body':raw,'headers':dict(self.headers),'committed':True})
            if not poll:
                self.states.setdefault(oid,{'status':'pending','polls':0})
            state=self.states.get(oid)
            if poll and state:state['polls']+=1
            snapshot=dict(state) if state else None
        code=200
        if not poll and self.path=='/placement_failed':code=400;body={'error':f.SECRET}
        elif snapshot is None:code=404;body={'status':'not_found'}
        elif poll and self.path=='/transient_poll' and snapshot['polls']==1:code=503;body={}
        elif poll and self.path=='/malformed_poll':body={'status':'fulfilled','reference':f'SUP-{oid}'}
        elif poll and snapshot['status']=='fulfilled':body={'status':'fulfilled','items':[f'WORKER-CODE-{i} 🧩' for i in range(p['quantity'])]}
        elif poll and snapshot['status']=='failed':body={'status':'failed'}
        else:code=202;body={'status':'pending','reference':f'SUP-{oid}'}
        encoded=json.dumps(body,ensure_ascii=False).encode()
        try:
            self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(encoded)));self.end_headers();self.wfile.write(encoded)
        except (BrokenPipeError,ConnectionResetError,ssl.SSLError):pass

class Phase16SupplierFulfillmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        f.Phase16FulfillmentWiringTests.setUpClass()
        f.Phase16FulfillmentWiringTests.server.RequestHandlerClass=SupplierStub
    @classmethod
    def tearDownClass(cls):f.Phase16FulfillmentWiringTests.tearDownClass()
    def setUp(self):
        self.fx=f.Phase16FulfillmentWiringTests('test_success_encrypted_real_delivery_and_committed_http')
        self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.orders=[];self.addCleanup(self.finish_pending)
    def finish_pending(self):
        for oid in self.orders:
            if api_order_status(oid)=='processing':handle_post_debit_failure(oid,reason='test fixture cleanup')
    def create(self,path='/supplier',quantity=1,ftype='supplier_api'):
        fixture=self.fx.product(path,ftype=ftype,supplier_product_id='TEST-PRODUCT')
        oid,data=self.fx.create(fixture,quantity);self.orders.append(oid)
        self.assertEqual(data['fulfillment']['status'],'pending')
        return fixture,oid
    def state(self,oid,status):
        with SupplierStub.lock:SupplierStub.states[oid]['status']=status
    def due(self,oid):w._delete_state(oid)
    def completed(self,fixture,oid,quantity=1):
        self.assertEqual(api_order_status(oid),'completed')
        token=self.fx.scalar('SELECT delivery FROM orders WHERE id=?',(oid,))
        self.assertTrue(token.startswith('enc:'));self.assertNotIn('WORKER-CODE',token)
        self.assertEqual(decrypt_text(token),'\n\n'.join(f'WORKER-CODE-{i} 🧩' for i in range(quantity)))
        self.assertEqual(self.fx.fx.read(oid,fixture[3])['items'],[f'WORKER-CODE-{i} 🧩' for i in range(quantity)])
        self.assertEqual(self.fx.events(oid,'delivery.completed'),1)
        self.assertEqual(f.reconcile_reseller(fixture[0]['id']),[])
    def test_placement_reference_encrypted_and_replay(self):
        fixture,oid=self.create()
        ref=self.fx.scalar('SELECT fulfillment_ref FROM orders WHERE id=?',(oid,))
        self.assertTrue(ref.startswith('enc:'));self.assertNotIn(f'SUP-{oid}',ref)
        self.assertEqual(decode_reference(ref),{'v':1,'kind':'supplier','value':f'SUP-{oid}'})
        self.assertIsNone(self.fx.scalar('SELECT delivery FROM orders WHERE id=?',(oid,)))
        response=self.fx.api.post('/api/v1/orders',headers=fixture[3],json={'product_id':fixture[2],'quantity':1,'client_order_id':fixture[4]})
        self.assertEqual(response.status_code,200);self.assertEqual(len(self.fx.calls(oid)),1)
    def test_real_delivery_endpoint_and_exactly_one_completion(self):
        fixture,oid=self.create(quantity=2);self.state(oid,'fulfilled')
        self.assertTrue(w._process_order(oid));self.completed(fixture,oid,2)
        calls=len(self.fx.calls(oid));self.assertFalse(w._process_order(oid));self.completed(fixture,oid,2)
        self.assertEqual(len(self.fx.calls(oid)),calls)
    def test_failure_refund_stock_and_notification(self):
        fixture,oid=self.create();self.state(oid,'failed')
        with patch.object(w,'notify_order_failed',return_value=(False,'unavailable')) as notify:
            self.assertTrue(w._process_order(oid));self.assertFalse(w._process_order(oid))
            self.assertEqual(notify.call_count,1);self.assertEqual(notify.call_args.args[3],'order failed, refunded')
        self.fx.assert_refund(fixture[0],oid)
        self.assertEqual(self.fx.scalar('SELECT stock FROM products WHERE id=?',(fixture[1],)),10)
    def test_maximum_age_cutoff_without_supplier_poll(self):
        fixture,oid=self.create();calls=len(self.fx.calls(oid))
        old=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(seconds=w.MAX_AGE_SECONDS+5)).strftime('%Y-%m-%d %H:%M:%S')
        f.db.cur.execute('UPDATE orders SET created=? WHERE id=?',(old,oid));f.db.conn.commit()
        with patch.object(w,'notify_order_failed',return_value=(True,'sent')):self.assertTrue(w._process_order(oid))
        self.fx.assert_refund(fixture[0],oid);self.assertEqual(len(self.fx.calls(oid)),calls)
    def test_shop_timezone_does_not_prematurely_expire_utc_creation(self):
        fixture,oid=self.create();clock=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5,minutes=30)))
        with patch.object(w,'_clock',return_value=clock):self.assertFalse(w._expired(w._read(oid)))
    def test_crash_after_poll_before_persistence_then_resume(self):
        fixture,oid=self.create();self.state(oid,'fulfilled')
        with patch.object(w,'complete_manual_delivery',side_effect=RuntimeError(f.SECRET)):
            self.assertFalse(w._process_order(oid))
        self.assertEqual(api_order_status(oid),'processing');self.assertEqual(self.fx.events(oid,'delivery.completed'),0)
        self.due(oid);self.assertTrue(w._process_order(oid));self.completed(fixture,oid)
    def test_outbox_failure_rolls_back_delivery_and_resumes(self):
        fixture,oid=self.create();self.state(oid,'fulfilled')
        with patch('reseller_api.services.webhook_outbox.emit_event_atomic',side_effect=RuntimeError(f.SECRET)):
            self.assertFalse(w._process_order(oid))
        self.assertIsNone(self.fx.scalar('SELECT delivery FROM orders WHERE id=?',(oid,)))
        self.due(oid);self.assertTrue(w._process_order(oid));self.completed(fixture,oid)
    def test_administrator_failure_is_skipped(self):
        fixture,oid=self.create();handle_post_debit_failure(oid,reason='admin fail');calls=len(self.fx.calls(oid))
        self.assertFalse(w._process_order(oid));self.assertEqual(len(self.fx.calls(oid)),calls);self.fx.assert_refund(fixture[0],oid)
    def test_cancelled_order_is_skipped(self):
        fixture,oid=self.create()
        from reseller_api.services.order_failure import handle_cancellation
        handle_cancellation(oid,'admin cancel',refund_if_debited=True)
        calls=len(self.fx.calls(oid));self.assertFalse(w._process_order(oid));self.assertEqual(len(self.fx.calls(oid)),calls)
    def test_transient_poll_then_success_durable_backoff(self):
        fixture,oid=self.create('/transient_poll');self.state(oid,'fulfilled')
        self.assertFalse(w._process_order(oid));self.assertGreater(int(w.get_setting(f'fulfillment.next.{oid}')),w._epoch())
        self.assertNotIn(oid,w._poll_candidates());self.due(oid)
        self.assertTrue(w._process_order(oid));self.completed(fixture,oid)
    def test_malformed_success_remains_pending(self):
        fixture,oid=self.create('/malformed_poll')
        self.assertFalse(w._process_order(oid));self.assertEqual(api_order_status(oid),'processing')
        self.assertIsNone(self.fx.scalar('SELECT delivery FROM orders WHERE id=?',(oid,)))
        self.assertEqual(self.fx.events(oid,'delivery.completed'),0);self.assertEqual(f.rdb.get_wallet_balance_cents(fixture[0]['id']),49000)
    def test_stale_claim_can_be_recovered(self):
        fixture,oid=self.create();self.state(oid,'fulfilled')
        f.db.cur.execute('INSERT INTO worker_jobs(job_key,worker_id,claimed_at,heartbeat_at) VALUES(?,?,0,0)',(f'fulfillment-job:{oid}','dead-worker'));f.db.conn.commit()
        self.assertTrue(w._process_order(oid));self.completed(fixture,oid)
    def test_active_claim_blocks_duplicate_worker(self):
        fixture,oid=self.create();self.state(oid,'fulfilled');calls=len(self.fx.calls(oid))
        self.assertTrue(w.claim_worker_job(f'fulfillment-job:{oid}','other-worker',ttl_seconds=w.CLAIM_TTL_SECONDS))
        self.assertFalse(w._process_order(oid));self.assertEqual(len(self.fx.calls(oid)),calls)
        f.db.cur.execute('DELETE FROM worker_jobs WHERE job_key=?',(f'fulfillment-job:{oid}',));f.db.conn.commit()
        self.assertTrue(w._process_order(oid));self.completed(fixture,oid)
    def test_lost_reference_recovers_using_order_id(self):
        fixture=self.fx.product('/supplier',ftype='supplier_api',supplier_product_id='TEST-PRODUCT')
        with patch('fulfillment.references.persist_reference',side_effect=RuntimeError(f.SECRET)):oid,data=self.fx.create(fixture)
        self.orders.append(oid);ref=decode_reference(self.fx.scalar('SELECT fulfillment_ref FROM orders WHERE id=?',(oid,)))
        self.assertEqual(ref['kind'],'order');self.state(oid,'fulfilled')
        self.assertTrue(w._process_order(oid));self.completed(fixture,oid)
        self.assertEqual(sum(c['payload'].get('action')!='status' for c in self.fx.calls(oid)),1)
    def test_missing_reference_bootstrapped_bounded(self):
        fixture,oid=self.create();f.db.cur.execute('UPDATE orders SET fulfillment_ref=NULL WHERE id=?',(oid,));f.db.conn.commit()
        w._bootstrap_missing_references();self.assertEqual(decode_reference(self.fx.scalar('SELECT fulfillment_ref FROM orders WHERE id=?',(oid,)))['kind'],'order')
        self.state(oid,'fulfilled');self.assertTrue(w._process_order(oid));self.completed(fixture,oid)
    def test_generic_pending_recovered(self):
        fixture,oid=self.create(ftype='external_api');self.state(oid,'fulfilled')
        self.assertTrue(w._process_order(oid));self.completed(fixture,oid)
    def test_failure_intent_repairs_crash_after_status(self):
        fixture,oid=self.create();self.state(oid,'failed')
        with patch('reseller_api.services.order_failure._post_refund_serialized',side_effect=RuntimeError('simulated crash')):
            self.assertFalse(w._process_order(oid))
        self.assertEqual(api_order_status(oid),'failed')
        self.assertIsNotNone(w.get_setting(f'fulfillment.failure.{oid}'))
        with patch.object(w,'notify_order_failed',return_value=False):self.assertTrue(w._process_order(oid))
        self.fx.assert_refund(fixture[0],oid);self.assertIsNone(w.get_setting(f'fulfillment.failure.{oid}'))
    def test_worker_logs_do_not_expose_credentials_or_reference(self):
        fixture,oid=self.create('/malformed_poll')
        with self.assertLogs(w.log,level='INFO') as logs:self.assertFalse(w._process_order(oid))
        text='\n'.join(logs.output)
        for secret in (f.SECRET,f.HMAC_SECRET,f'SUP-{oid}','supplier.example'):self.assertNotIn(secret,text)
    def test_private_ip_status_request_refused(self):
        fixture,oid=self.create();calls=len(self.fx.calls(oid))
        with patch.object(f.generic,'resolve_public_ips',side_effect=f.generic.FulfillmentConfigError()):self.assertTrue(w._process_order(oid))
        self.fx.assert_refund(fixture[0],oid);self.assertEqual(len(self.fx.calls(oid)),calls)
    def concurrent(self,oid):
        barrier=threading.Barrier(4);errors=[];outcomes=[]
        def process():
            try:
                barrier.wait(timeout=5);outcomes.append(w._process_order(oid))
            except Exception as error:errors.append(type(error).__name__)
            finally:f.db.close_thread_connection()
        threads=[threading.Thread(target=process) for _ in range(4)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(15)
        self.assertFalse(any(thread.is_alive() for thread in threads));self.assertEqual(errors,[])
        self.assertEqual(sum(bool(x) for x in outcomes),1)
    def test_concurrent_workers_complete_exactly_once(self):
        fixture,oid=self.create();self.state(oid,'fulfilled');self.concurrent(oid);self.completed(fixture,oid)
        self.assertEqual(sum(c['payload'].get('action')=='status' for c in self.fx.calls(oid)),1)
    def test_concurrent_workers_refund_exactly_once(self):
        fixture,oid=self.create();self.state(oid,'failed')
        with patch.object(w,'notify_order_failed',return_value=False):self.concurrent(oid)
        self.fx.assert_refund(fixture[0],oid)
        self.assertEqual(self.fx.scalar('SELECT stock FROM products WHERE id=?',(fixture[1],)),10)
    def test_owner_action_lease_blocks_completion_then_resume(self):
        fixture,oid=self.create();self.state(oid,'fulfilled')
        key=f'admin-order-lifecycle:{oid}';self.assertTrue(w.claim_worker_job(key,'admin-working'))
        self.assertFalse(w._process_order(oid));self.assertEqual(api_order_status(oid),'processing')
        f.db.cur.execute('DELETE FROM worker_jobs WHERE job_key=?',(key,));f.db.conn.commit()
        self.due(oid);self.assertTrue(w._process_order(oid));self.completed(fixture,oid)
    def test_no_attempt_busy_dispatch_recovers_with_same_placement_key(self):
        from fulfillment.router import _DISPATCH_SLOTS
        fixture=self.fx.product('/supplier',ftype='supplier_api',supplier_product_id='TEST-PRODUCT')
        acquired=0
        try:
            while _DISPATCH_SLOTS.acquire(blocking=False):acquired+=1
            oid,data=self.fx.create(fixture);self.orders.append(oid)
        finally:
            for _ in range(acquired):_DISPATCH_SLOTS.release()
        self.assertEqual(len(self.fx.calls(oid)),0)
        self.assertFalse(w._process_order(oid))
        placement=[c for c in self.fx.calls(oid) if c['payload'].get('action')!='status']
        self.assertEqual(len(placement),1);self.assertEqual(placement[0]['headers']['Idempotency-Key'],f'reseller-order:{oid}')
        self.state(oid,'fulfilled');self.due(oid);self.assertTrue(w._process_order(oid));self.completed(fixture,oid)
    def test_status_polls_not_cached_by_placement_idempotency_header(self):
        fixture,oid=self.create();self.assertFalse(w._process_order(oid))
        poll=self.fx.calls(oid)[-1];self.assertEqual(poll['payload']['action'],'status')
        self.assertNotIn('Idempotency-Key',poll['headers'])
        self.state(oid,'fulfilled');self.due(oid);self.assertTrue(w._process_order(oid));self.completed(fixture,oid)
    def test_reference_is_not_overwritten_by_different_supplier(self):
        from fulfillment.references import persist_reference,ReferenceUnavailable
        fixture,oid=self.create();before=self.fx.scalar('SELECT fulfillment_ref FROM orders WHERE id=?',(oid,))
        with self.assertRaises(ReferenceUnavailable):persist_reference(oid,'OTHER-SUPPLIER-REFERENCE')
        self.assertEqual(self.fx.scalar('SELECT fulfillment_ref FROM orders WHERE id=?',(oid,)),before)
    def test_invalid_supplier_config_missing_fields(self):
        from fulfillment.external_clients.supplier_api_client import validate_supplier_config
        for cfg in ({'endpoint':'https://supplier.example','api_key':'synthetic'},
                    {'endpoint':'https://supplier.example','supplier_product_id':'TEST-PRODUCT'}):
            with self.assertRaises(f.generic.FulfillmentConfigError):validate_supplier_config(cfg,resolve=False)
    def test_no_linked_telegram_skips_notification(self):
        fixture,oid=self.create();self.state(oid,'failed')
        f.db.cur.execute('UPDATE resellers SET telegram_user_id=NULL WHERE id=?',(fixture[0]['id'],));f.db.conn.commit()
        with patch.object(w,'notify_order_failed') as notify:self.assertTrue(w._process_order(oid));notify.assert_not_called()
        self.fx.assert_refund(fixture[0],oid)
    def test_actual_notification_helper_handles_exception(self):
        from reseller_api.services.order_notifications import notify_order_failed
        def unavailable(*args):raise RuntimeError('network unavailable')
        self.assertFalse(notify_order_failed(1,1,{'product_name':'test'},'order failed, refunded',100,send=unavailable))
    def test_due_query_bounded_and_max_age_overrides_backoff(self):
        fixture,oid=self.create();w._schedule(oid)
        self.assertNotIn(oid,w._poll_candidates())
        old=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(seconds=w.MAX_AGE_SECONDS+10)).strftime('%Y-%m-%d %H:%M:%S')
        f.db.cur.execute('UPDATE orders SET created=? WHERE id=?',(old,oid));f.db.conn.commit()
        candidates=w._poll_candidates();self.assertLessEqual(len(candidates),w.BATCH_SIZE);self.assertIn(oid,candidates)
    def test_run_once_completes_real_pending_order(self):
        fixture,oid=self.create();self.state(oid,'fulfilled')
        self.assertGreaterEqual(w.run_once(),1);self.completed(fixture,oid)
        w.run_once();self.completed(fixture,oid)
    def test_schema_reference_nullable_without_new_reference_index(self):
        f.db.cur.execute('PRAGMA table_info(orders)');cols={r[1]:r for r in f.db.cur.fetchall()}
        self.assertEqual(cols['fulfillment_ref'][2],'TEXT');self.assertEqual(cols['fulfillment_ref'][3],0)
        f.db.cur.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='orders'")
        self.assertFalse(any('fulfillment_ref' in (r[0] or '') for r in f.db.cur.fetchall()))
    def test_initial_dispatch_refund_crash_is_repaired_by_worker(self):
        fixture=self.fx.product('/placement_failed',ftype='supplier_api',supplier_product_id='TEST-PRODUCT')
        with patch('reseller_api.services.order_failure._post_refund_serialized',side_effect=RuntimeError('simulated crash')):
            oid,data=self.fx.create(fixture)
        self.orders.append(oid);self.assertEqual(data['status'],'failed')
        self.assertEqual(f.rdb.get_wallet_balance_cents(fixture[0]['id']),49000)
        self.assertIsNotNone(w.get_setting(f'fulfillment.failure.{oid}'))
        calls=len(self.fx.calls(oid))
        with patch.object(w,'notify_order_failed',return_value=False):self.assertTrue(w._process_order(oid))
        self.fx.assert_refund(fixture[0],oid);self.assertEqual(len(self.fx.calls(oid)),calls)
    def test_standalone_cli_initializes_and_completes_empty_batch(self):
        import os,subprocess,tempfile,pathlib
        root=str(pathlib.Path(__file__).resolve().parents[1]);env=os.environ.copy()
        env.update(DATABASE_URL='',BOT_TOKEN='',ADMIN_ID='0',RESELLER_API_REQUIRE_POSTGRES='0',DATA_ENCRYPTION_KEY='isolated-cli-test-key',PYTHONPATH=root+os.pathsep+env.get('PYTHONPATH',''))
        with tempfile.TemporaryDirectory(prefix='fulfillment_cli_') as tmp:
            result=subprocess.run(['python','-m','reseller_api.workers.fulfillment_worker','--once'],cwd=tmp,env=env,capture_output=True,text=True,timeout=25)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            import sqlite3
            with sqlite3.connect(str(pathlib.Path(tmp)/'shopbot.db')) as conn:
                self.assertIn('fulfillment_ref',[r[1] for r in conn.execute('PRAGMA table_info(orders)')])
    def test_standalone_cli_fails_closed_before_sqlite_creation(self):
        import os,subprocess,tempfile,pathlib
        root=str(pathlib.Path(__file__).resolve().parents[1]);env=os.environ.copy()
        env.update(DATABASE_URL='',RESELLER_API_REQUIRE_POSTGRES='1',DATA_ENCRYPTION_KEY='isolated-cli-test-key',PYTHONPATH=root+os.pathsep+env.get('PYTHONPATH',''))
        with tempfile.TemporaryDirectory(prefix='fulfillment_refused_') as tmp:
            result=subprocess.run(['python','-m','reseller_api.workers.fulfillment_worker','--once'],cwd=tmp,env=env,capture_output=True,text=True,timeout=10)
            self.assertNotEqual(result.returncode,0);self.assertIn('startup refused',result.stdout)
            self.assertFalse((pathlib.Path(tmp)/'shopbot.db').exists())
    def test_partial_admin_refund_caps_automatic_failure(self):
        from reseller_api.services.adjustment_service import issue_admin_refund
        fixture,oid=self.create();issue_admin_refund(oid,400,'test goodwill',actor='owner');self.state(oid,'failed')
        with patch.object(w,'notify_order_failed',return_value=False):self.assertTrue(w._process_order(oid))
        self.fx.assert_refund(fixture[0],oid)
        self.assertEqual(self.fx.scalar('SELECT amount_cents FROM reseller_wallet_transactions WHERE reference=?',(f'rapi.refund.order.{oid}',)),600)
    def test_corrupt_reference_never_guessed_or_polled(self):
        fixture,oid=self.create();calls=len(self.fx.calls(oid))
        f.db.cur.execute('UPDATE orders SET fulfillment_ref=? WHERE id=?',('enc:corrupt-test-reference',oid));f.db.conn.commit()
        self.assertFalse(w._process_order(oid));self.assertEqual(len(self.fx.calls(oid)),calls)
        self.assertEqual(api_order_status(oid),'processing')
    def test_supplier_missing_product_id_rejected_at_owner_save(self):
        fixture,oid=self.create()
        response=self.fx.owner.post(f'/api/products/{fixture[1]}/update',headers={'X-CSRF-Token':'test-csrf'},
            json={'fulfillment_type':'supplier_api','fulfillment_config':{'endpoint':'https://supplier.example','api_key':f.SECRET}})
        self.assertEqual(response.status_code,400)
        self.assertNotIn(f.SECRET,response.get_data(as_text=True))
    def test_internal_reference_not_in_api_or_webadmin_order_response(self):
        fixture,oid=self.create();reference=self.fx.scalar('SELECT fulfillment_ref FROM orders WHERE id=?',(oid,))
        response=self.fx.api.get(f'/api/v1/orders/ORD-{oid:08d}',headers=fixture[3])
        self.assertEqual(response.status_code,200)
        admin=self.fx.owner.get('/api/orders');self.assertEqual(admin.status_code,200)
        text=response.get_data(as_text=True)+admin.get_data(as_text=True)
        for sensitive in ('fulfillment_ref',reference,f'SUP-{oid}',f.SECRET,f.HMAC_SECRET):self.assertNotIn(sensitive,text)
    def test_runtime_requires_encryption_and_shared_postgres(self):
        import os
        with patch.dict(os.environ,{'DATA_ENCRYPTION_KEY':'','RESELLER_API_REQUIRE_POSTGRES':'0'}):
            with self.assertRaises(RuntimeError):w._validate_runtime()
        with patch.dict(os.environ,{'DATA_ENCRYPTION_KEY':'synthetic','RESELLER_API_REQUIRE_POSTGRES':'1','DATABASE_URL':''}):
            with self.assertRaises(RuntimeError):w._validate_runtime()
    def test_worker_claim_keys_distinct_from_webhook(self):
        fixture,oid=self.create();self.state(oid,'fulfilled')
        with patch.object(w,'claim_worker_job',wraps=w.claim_worker_job) as claim:self.assertTrue(w._process_order(oid))
        keys=[c.args[0] for c in claim.call_args_list]
        self.assertIn(f'fulfillment-job:{oid}',keys);self.assertTrue(all('webhook' not in key for key in keys))

if __name__=='__main__':unittest.main()
