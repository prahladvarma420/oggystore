"""Private API invites: actual routes/queue/accounting, mocked legacy browser boundary."""
import asyncio
import contextlib
import datetime
import io
import json
import logging
import os
import time
import unittest
from unittest.mock import patch,AsyncMock
import tests.test_phase16_fulfillment_wiring as f
from services import api_email_invites as lane,pw_lane,m365_family,canva_invite
from reseller_api.services.order_status import api_order_status
from utils.crypto import decrypt_text

EMAIL='private.buyer+api@example.org'
OTHER='different.buyer@example.org'

class Phase16EmailProductsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):f.Phase16FulfillmentWiringTests.setUpClass()
    @classmethod
    def tearDownClass(cls):f.Phase16FulfillmentWiringTests.tearDownClass()
    def setUp(self):
        self.fx=f.Phase16FulfillmentWiringTests('test_success_encrypted_real_delivery_and_committed_http');self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.orders=[];self.addCleanup(self.cleanup_orders)
        self.env=patch.dict(os.environ,{'DEBUG':'','PWDEBUG':'','DEBUGP':''});self.env.start();self.addCleanup(self.env.stop)
    def cleanup_orders(self):
        for oid in self.orders:
            if api_order_status(oid)=='processing':f.handle_post_debit_failure(oid,reason='isolated test cleanup')
    def fixture(self,kind='m365'):
        fixture=self.fx.product(ftype='manual')
        f.db.set_setting(f'{kind}_invite_products',str(fixture[1]))
        return fixture
    def request(self,fixture,email=EMAIL,quote=False,quantity=1):
        body={'product_id':fixture[2],'quantity':quantity,'client_order_id':fixture[4]}
        if email is not MISSING:body['buyer_email']=email
        return self.fx.api.post('/api/v1/orders'+('/quote' if quote else ''),headers=fixture[3],json=body)
    def create(self,kind='m365',email=EMAIL):
        fixture=self.fixture(kind)
        with patch('fulfillment.order_dispatch.route_fulfillment') as external:
            response=self.request(fixture,email);external.assert_not_called()
        self.assertEqual(response.status_code,201,response.get_json());data=response.get_json()['data'];oid=int(data['order_id'].replace('ORD-',''))
        self.orders.append(oid)
        self.assertEqual((data['status'],data['delivery_status']),('processing','pending'))
        self.assertIsNone(self.fx.scalar('SELECT delivery FROM orders WHERE id=?',(oid,)))
        f.db.cur.execute(f'SELECT id,email FROM {kind}_invites WHERE order_id=?',(oid,));row=f.db.cur.fetchone()
        self.assertIsNotNone(row);self.assertTrue(row[1].startswith('enc:'));self.assertEqual(decrypt_text(row[1]),email)
        return fixture,oid,int(row[0])
    def process(self,kind,iid):return asyncio.run(lane.process_invite(kind,iid))
    def complete(self,fixture,oid):
        self.assertEqual(api_order_status(oid),'completed')
        self.assertEqual(self.fx.events(oid,'delivery.completed'),1)
        delivery=self.fx.fx.read(oid,fixture[3]);self.assertTrue(delivery['items']);self.assertNotIn(EMAIL,json.dumps(delivery))
        self.assertTrue(self.fx.scalar('SELECT delivery FROM orders WHERE id=?',(oid,)).startswith('enc:'))
        self.assertEqual(f.reconcile_reseller(fixture[0]['id']),[])
    def test_missing_email_422_both_products(self):
        for kind in ('m365','canva'):
            fixture=self.fixture(kind);response=self.request(fixture,MISSING)
            self.assertEqual(response.status_code,422);self.assertEqual(response.get_json()['error']['code'],'buyer_email_required')
    def test_email_validation_matrix_and_no_email_in_errors(self):
        fixture=self.fixture()
        for email in ('','not-an-email','a..b@example.org','.a@example.org','a.@example.org','a@-bad.org','a@example..org','a@example.org\r\nBcc:x@y.org','a'*65+'@example.org','a@'+'a'*245+'.org',None,[],42,' buyer@example.org','buyer@example.org ','"quoted"@example.org'):
            response=self.request(fixture,email);self.assertEqual(response.status_code,400,(email,response.get_json()))
            self.assertEqual(response.get_json()['error']['code'],'invalid_parameter')
            self.assertNotIn('Bcc:',response.get_data(as_text=True))
    def test_normal_product_rejects_supplied_email_even_null(self):
        fixture=self.fx.product(ftype='manual')
        for email in (EMAIL,None,'malformed'):
            response=self.request(fixture,email);self.assertEqual(response.status_code,400)
            self.assertEqual(response.get_json()['error']['message'],'not accepted for this product')
    def test_m365_pending_then_real_endpoint_completion(self):
        fixture,oid,iid=self.create()
        with patch.object(m365_family,'_invite_with_failover_locked',return_value=(True,'sent',None,False)) as browser:
            self.assertTrue(self.process('m365',iid));self.assertEqual(browser.call_args.args[0],EMAIL)
        self.complete(fixture,oid)
    def test_canva_pending_then_completion(self):
        fixture,oid,iid=self.create('canva')
        with patch.object(canva_invite,'has_session',return_value=True),patch.object(canva_invite,'_send_canva_with_retries_locked',return_value=(True,'sent',None)) as browser:
            self.assertTrue(self.process('canva',iid));self.assertEqual(browser.call_args.args[0],EMAIL)
        self.complete(fixture,oid)
    def test_replay_different_email_keeps_original_ciphertext_and_one_queue_row(self):
        fixture,oid,iid=self.create();before=self.fx.scalar('SELECT email FROM m365_invites WHERE id=?',(iid,))
        response=self.request(fixture,OTHER);self.assertEqual(response.status_code,200)
        self.assertEqual(response.get_json()['data']['order_id'],f'ORD-{oid:08d}')
        self.assertEqual(self.fx.scalar('SELECT email FROM m365_invites WHERE id=?',(iid,)),before)
        self.assertEqual(self.fx.scalar('SELECT COUNT(*) FROM m365_invites WHERE order_id=?',(oid,)),1)
        self.assertEqual(f.rdb.get_wallet_balance_cents(fixture[0]['id']),49000)
    def test_quotes_require_email_flag_and_no_side_effects(self):
        for kind in ('m365','canva'):
            fixture=self.fixture(kind);before=self.fx.scalar('SELECT COUNT(*) FROM orders')
            with patch.object(lane,'enqueue_atomic') as enqueue,patch.object(lane,'process_invite') as process:
                response=self.request(fixture,quote=True);self.assertEqual(response.status_code,200,response.get_json())
                enqueue.assert_not_called();process.assert_not_called()
            data=response.get_json()['data'];self.assertTrue(data['requires_buyer_email']);self.assertEqual(data['max_per_order'],1)
            self.assertEqual((data['unit_price'],data['total']),('10.00','10.00'))
            self.assertNotIn('status',data);self.assertNotIn('buyer_email',data)
            self.assertEqual(self.fx.scalar('SELECT COUNT(*) FROM orders'),before);self.assertEqual(f.rdb.get_wallet_balance_cents(fixture[0]['id']),50000)
    def test_quote_missing_email_422_and_normal_quote_unchanged(self):
        fixture=self.fixture();self.assertEqual(self.request(fixture,MISSING,quote=True).status_code,422)
        normal=self.fx.product(ftype='manual');response=self.request(normal,MISSING,quote=True)
        self.assertEqual(response.status_code,200);self.assertNotIn('requires_buyer_email',response.get_json()['data'])
    def test_lane_busy_parks_without_stacked_browser_thread_then_retries(self):
        fixture,oid,iid=self.create();self.assertTrue(pw_lane.acquire('synthetic busy'))
        try:
            with patch.object(m365_family,'_invite_with_failover_locked') as browser,patch('services.api_email_invites.asyncio.to_thread') as thread:
                self.assertFalse(self.process('m365',iid));browser.assert_not_called();thread.assert_not_called()
        finally:pw_lane.release()
        with patch.object(m365_family,'_invite_with_failover_locked',return_value=(True,'sent',None,False)):self.assertTrue(self.process('m365',iid))
        self.complete(fixture,oid)
    def test_permanently_busy_lane_still_age_fails_refunds(self):
        fixture,oid,iid=self.create();old=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
        f.db.cur.execute('UPDATE orders SET created=? WHERE id=?',(old,oid));f.db.conn.commit();self.assertTrue(pw_lane.acquire('hung task'))
        try:
            with patch.object(m365_family,'_invite_with_failover_locked') as browser:
                self.assertTrue(self.process('m365',iid));browser.assert_not_called()
        finally:pw_lane.release()
        self.fx.assert_refund(fixture[0],oid);self.assertEqual(self.fx.scalar('SELECT stock FROM products WHERE id=?',(fixture[1],)),10)
    def test_permanent_failure_refund_exactly_once(self):
        fixture,oid,iid=self.create('canva')
        with patch.object(canva_invite,'has_session',return_value=True),patch.object(canva_invite,'_send_canva_with_retries_locked',return_value=(False,EMAIL,None)):
            self.assertTrue(self.process('canva',iid));self.assertFalse(self.process('canva',iid))
        self.fx.assert_refund(fixture[0],oid)
    def test_no_sessions_park_not_immediate_refund(self):
        fixture,oid,iid=self.create()
        with patch.object(m365_family,'_invite_with_failover_locked',return_value=(False,'session expired '+EMAIL,None,True)):self.assertFalse(self.process('m365',iid))
        self.assertEqual(api_order_status(oid),'processing');self.assertEqual(self.fx.scalar('SELECT error FROM m365_invites WHERE id=?',(iid,)),None)
        fixture,oid,iid=self.create('canva')
        with patch.object(canva_invite,'has_session',return_value=False),patch.object(canva_invite,'_send_canva_with_retries_locked') as browser:
            self.assertFalse(self.process('canva',iid));browser.assert_not_called()
    def test_no_api_wait_for_browser_automation(self):
        fixture=self.fixture()
        with patch.object(m365_family,'_invite_with_failover_locked',side_effect=AssertionError('must not run in HTTP response')) as browser:
            started=time.monotonic();response=self.request(fixture);elapsed=time.monotonic()-started
            self.assertEqual(response.status_code,201);browser.assert_not_called();self.assertLess(elapsed,1)
        oid=int(response.get_json()['data']['order_id'].replace('ORD-',''));self.orders.append(oid)
    def test_sent_receipt_resumes_local_outbox_failure_without_second_invite(self):
        fixture,oid,iid=self.create()
        with patch.object(m365_family,'_invite_with_failover_locked',return_value=(True,'sent',None,False)) as browser:
            with patch('reseller_api.services.webhook_outbox.emit_event_atomic',side_effect=RuntimeError(EMAIL)):
                self.assertFalse(self.process('m365',iid))
            self.assertEqual(api_order_status(oid),'processing');self.assertEqual(lane._read('m365',iid)['status'],'api_sent')
            self.assertTrue(self.process('m365',iid));self.assertEqual(browser.call_count,1)
        self.complete(fixture,oid)
    def test_ambiguous_remote_exception_not_blindly_reinvited(self):
        fixture,oid,iid=self.create()
        with patch.object(m365_family,'_invite_with_failover_locked',side_effect=RuntimeError(EMAIL)) as browser:
            self.assertFalse(self.process('m365',iid));self.assertFalse(self.process('m365',iid));self.assertEqual(browser.call_count,1)
        self.assertEqual(lane._read('m365',iid)['status'],'api_uncertain');self.assertEqual(api_order_status(oid),'processing')
    def test_redaction_scan_real_responses_events_exports_analytics_logs(self):
        fixture,oid,iid=self.create();stream=io.StringIO();logger=logging.getLogger();handler=logging.StreamHandler(stream);logger.addHandler(handler)
        structured=io.StringIO()
        class Capture(logging.Handler):
            def emit(self,record):structured.write(json.dumps(record.__dict__,default=str))
        private_logger=logging.getLogger('p16-email-structured');capture=Capture();private_logger.addHandler(capture);self.addCleanup(lambda:private_logger.removeHandler(capture))
        def browser(email,*args):
            private_logger.warning('email diagnostic',extra={'buyer_email':email,'payload':{'recipient':email}})
            m365_family._log('email='+email);logging.warning('buyer_email=%s',email)
            from utils.error_logger import log_error
            try:raise RuntimeError(email)
            except Exception as error:log_error('email '+email,error,extra=email)
            return True,email,None,False
        output=io.StringIO()
        try:
            with contextlib.redirect_stdout(output),patch.object(m365_family,'_invite_with_failover_locked',side_effect=browser):self.assertTrue(self.process('m365',iid))
        finally:logger.removeHandler(handler)
        f.db.cur.execute('SELECT payload_json FROM webhook_outbox WHERE aggregate_id=?',(f'ORD-{oid:08d}',));events=str(f.db.cur.fetchall())
        detail=self.fx.api.get(f'/api/v1/orders/ORD-{oid:08d}',headers=fixture[3]).get_data(as_text=True)
        listing=self.fx.api.get('/api/v1/orders',headers=fixture[3]).get_data(as_text=True)
        admin=self.fx.owner.get('/api/orders').get_data(as_text=True)
        export=self.fx.api.get('/api/v1/exports/orders.csv',headers=fixture[3])
        analytics=self.fx.api.get('/api/v1/analytics/sales',headers=fixture[3])
        self.assertEqual(export.status_code,200);self.assertEqual(analytics.status_code,200)
        text=stream.getvalue()+structured.getvalue()+output.getvalue()+events+detail+listing+admin+export.get_data(as_text=True)+analytics.get_data(as_text=True)
        self.assertNotIn(EMAIL,text);self.assertNotIn('private.buyer',text)
    def test_same_queue_and_no_schema_addition(self):
        fixture,oid,iid=self.create();f.db.cur.execute('PRAGMA table_info(orders)')
        self.assertNotIn('buyer_email',[r[1] for r in f.db.cur.fetchall()])
        self.assertEqual(self.fx.scalar('SELECT user_id FROM m365_invites WHERE id=?',(iid,)),0)
    def test_email_products_follow_existing_single_unit_telegram_rule(self):
        fixture=self.fixture();response=self.request(fixture,quantity=2)
        self.assertEqual(response.status_code,400);self.assertEqual(response.get_json()['error']['code'],'invalid_quantity')
    def test_debug_environment_cannot_launch_pii_protocol_logging(self):
        fixture,oid,iid=self.create()
        with patch.dict(os.environ,{'DEBUG':'pw:*'}),patch.object(m365_family,'_invite_with_failover_locked') as browser:
            self.assertFalse(self.process('m365',iid));browser.assert_not_called()
    def test_no_encryption_key_rolls_back_order_and_wallet(self):
        fixture=self.fixture()
        with patch.dict(os.environ,{'DATA_ENCRYPTION_KEY':''}):response=self.request(fixture)
        self.assertEqual(response.status_code,500);self.assertEqual(f.rdb.get_wallet_balance_cents(fixture[0]['id']),50000)
        self.assertEqual(self.fx.scalar('SELECT stock FROM products WHERE id=?',(fixture[1],)),10)
    def test_queue_insertion_failure_rolls_back_order_wallet_and_stock(self):
        fixture=self.fixture();before=self.fx.scalar('SELECT COUNT(*) FROM orders');original=lane.enqueue_atomic
        def fail(*args):original(*args);raise RuntimeError(EMAIL)
        with patch.object(lane,'enqueue_atomic',side_effect=fail):response=self.request(fixture)
        self.assertEqual(response.status_code,500);self.assertNotIn(EMAIL,response.get_data(as_text=True))
        self.assertEqual(self.fx.scalar('SELECT COUNT(*) FROM orders'),before)
        self.assertEqual(f.rdb.get_wallet_balance_cents(fixture[0]['id']),50000)
        self.assertEqual(self.fx.scalar('SELECT stock FROM products WHERE id=?',(fixture[1],)),10)
    def test_m365_warranty_group_preserved(self):
        fixture=self.fixture();f.db.set_m365_product_warranty(fixture[1],'6m')
        response=self.request(fixture);self.assertEqual(response.status_code,201)
        oid=int(response.get_json()['data']['order_id'].replace('ORD-',''));self.orders.append(oid)
        iid=self.fx.scalar('SELECT id FROM m365_invites WHERE order_id=?',(oid,))
        with patch.object(m365_family,'_invite_with_failover_locked',return_value=(True,'sent',None,False)) as browser:
            self.assertTrue(self.process('m365',iid));self.assertEqual(browser.call_args.args,(EMAIL,'6m'))
        self.complete(fixture,oid)
    def test_api_proof_screenshot_is_discarded(self):
        fixture,oid,iid=self.create()
        def browser(*args):m365_family._LAST_SCREENSHOT=EMAIL.encode();return True,'sent',EMAIL.encode(),False
        with patch.object(m365_family,'_invite_with_failover_locked',side_effect=browser):self.assertTrue(self.process('m365',iid))
        self.assertIsNone(m365_family._LAST_SCREENSHOT);self.complete(fixture,oid)
    def test_maintenance_drain_fair_cursor_completes_both_queues(self):
        a,oa,ia=self.create();b,ob,ib=self.create('canva')
        with patch.object(m365_family,'_invite_with_failover_locked',return_value=(True,'sent',None,False)),patch.object(canva_invite,'has_session',return_value=True),patch.object(canva_invite,'_send_canva_with_retries_locked',return_value=(True,'sent',None)):
            for _ in range(4):asyncio.run(lane.drain_pending(limit=1))
        self.complete(a,oa);self.complete(b,ob)
    def test_private_email_cannot_be_copied_into_public_client_order_id(self):
        fixture=list(self.fixture());fixture[4]=EMAIL
        response=self.request(fixture);self.assertEqual(response.status_code,400)
        self.assertNotIn(EMAIL,response.get_data(as_text=True))
    def test_simultaneous_api_jobs_only_one_browser_and_completion(self):
        fixture,oid,iid=self.create()
        async def together():return await asyncio.gather(lane.process_invite('m365',iid),lane.process_invite('m365',iid))
        with patch.object(m365_family,'_invite_with_failover_locked',return_value=(True,'sent',None,False)) as browser:
            result=asyncio.run(together());self.assertEqual(sum(bool(x) for x in result),1);self.assertEqual(browser.call_count,1)
        self.complete(fixture,oid)
    def test_admin_failed_order_skips_pending_invite(self):
        fixture,oid,iid=self.create();f.handle_post_debit_failure(oid,reason='owner fail')
        with patch.object(m365_family,'_invite_with_failover_locked') as browser:
            self.assertFalse(self.process('m365',iid));browser.assert_not_called()
        self.fx.assert_refund(fixture[0],oid)
    def test_financial_crash_is_repaired_by_existing_16p7_worker(self):
        from reseller_api.workers import fulfillment_worker as worker
        fixture,oid,iid=self.create()
        with patch.object(m365_family,'_invite_with_failover_locked',return_value=(False,'permanent',None,False)),patch('reseller_api.services.order_failure._post_refund_serialized',side_effect=RuntimeError(EMAIL)):
            self.assertFalse(self.process('m365',iid))
        self.assertEqual(api_order_status(oid),'failed');self.assertEqual(f.rdb.get_wallet_balance_cents(fixture[0]['id']),49000)
        self.assertTrue(worker._process_order(oid));self.fx.assert_refund(fixture[0],oid)
    def test_partial_admin_refund_is_respected_by_gated_failure(self):
        from reseller_api.services.adjustment_service import issue_admin_refund
        fixture,oid,iid=self.create();issue_admin_refund(oid,400,'test goodwill',actor='owner')
        with patch.object(m365_family,'_invite_with_failover_locked',return_value=(False,'permanent',None,False)):self.assertTrue(self.process('m365',iid))
        self.fx.assert_refund(fixture[0],oid)
        self.assertEqual(self.fx.scalar('SELECT amount_cents FROM reseller_wallet_transactions WHERE reference=?',(f'rapi.refund.order.{oid}',)),600)
    def test_bot_maintenance_calls_shared_api_queue_drain(self):
        from types import SimpleNamespace
        with patch.object(m365_family,'_keepalive_sessions_sync',return_value=[]),patch.object(f.db,'has_healthy_m365_session',return_value=False),patch.object(lane,'drain_pending',new=AsyncMock(return_value=0)) as drain:
            asyncio.run(m365_family.run_m365_maintenance(SimpleNamespace(bot=AsyncMock())));drain.assert_awaited_once()
    def test_confirmed_browser_failure_retries_refund_after_owner_claim_conflict(self):
        from reseller_api.workers import fulfillment_worker as worker
        fixture,oid,iid=self.create();key=f'admin-order-lifecycle:{oid}'
        self.assertTrue(worker.claim_worker_job(key,'synthetic-owner-action'))
        with patch.object(m365_family,'_invite_with_failover_locked',return_value=(False,'permanent',None,False)) as browser:
            self.assertFalse(self.process('m365',iid));self.assertEqual(lane._read('m365',iid)['status'],'api_rejected')
            f.db.cur.execute('DELETE FROM worker_jobs WHERE job_key=?',(key,));f.db.conn.commit()
            self.assertTrue(self.process('m365',iid));self.assertEqual(browser.call_count,1)
        self.fx.assert_refund(fixture[0],oid)
    def test_timeout_does_not_release_lane_until_browser_thread_exits(self):
        import threading
        fixture,oid,iid=self.create();started=threading.Event();release=threading.Event()
        def browser(*args):started.set();release.wait(5);return True,'sent',None,False
        async def timeout(awaitable,**kwargs):
            for _ in range(50):
                if started.is_set():break
                await asyncio.sleep(.01)
            raise asyncio.TimeoutError()
        async def run():
            self.assertFalse(await lane.process_invite('m365',iid));self.assertTrue(started.is_set())
            self.assertNotEqual(pw_lane.busy_info(),'idle')
            self.assertFalse(await lane.process_invite('m365',iid));release.set()
            for _ in range(100):
                if pw_lane.busy_info()=='idle':break
                await asyncio.sleep(.01)
        try:
            with patch.object(m365_family,'_invite_with_failover_locked',side_effect=browser) as mock,patch('services.api_email_invites.asyncio.wait_for',side_effect=timeout):asyncio.run(run())
            self.assertEqual(mock.call_count,1);self.complete(fixture,oid)
        finally:release.set()
    def test_existing_telegram_canva_process_still_calls_interactive_wrapper(self):
        bot=AsyncMock();invite=(100,12345,0,'telegram-customer@example.org','pending',None,'2026-01-01')
        with patch.object(f.db,'get_canva_invite',return_value=invite),patch.object(lane,'is_api_order',return_value=False),patch.object(canva_invite,'send_canva_invite',new=AsyncMock(return_value=(True,'sent',None))) as browser,patch.object(f.db,'set_canva_invite_status'):
            asyncio.run(canva_invite.process_invite(bot,100));browser.assert_awaited_once_with(invite[3]);self.assertTrue(bot.send_message.await_count)
    def test_existing_telegram_m365_process_still_calls_interactive_wrapper(self):
        bot=AsyncMock();invite=(100,12345,0,'telegram-customer@example.org','pending',None,'2026-01-01')
        with patch.object(f.db,'get_m365_invite',return_value=invite),patch.object(lane,'is_api_order',return_value=False),patch.object(m365_family,'send_family_invite_ex',new=AsyncMock(return_value=(True,'sent',None,False))) as browser,patch.object(f.db,'set_m365_invite_status'):
            asyncio.run(m365_family.process_invite(bot,100));self.assertEqual(browser.await_args.args[0],invite[3]);self.assertTrue(bot.send_message.await_count)
    def test_legacy_process_api_branch_never_sends_email_telegram_notifications(self):
        fixture,oid,iid=self.create('canva');bot=AsyncMock()
        with patch.object(canva_invite,'has_session',return_value=True),patch.object(canva_invite,'_send_canva_with_retries_locked',return_value=(True,'sent',None)):
            asyncio.run(canva_invite.process_invite(bot,iid))
        bot.send_message.assert_not_awaited();bot.send_photo.assert_not_awaited();self.complete(fixture,oid)

MISSING=object()
if __name__=='__main__':unittest.main()
