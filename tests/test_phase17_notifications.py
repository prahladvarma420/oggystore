"""17.5 notifications: real business routes/ledger + isolated Telegram seam.

No real BOT_TOKEN/provider. Tests use established temporary SQLite fixtures,
queued job capture, a mocked telegram.Bot and actual daemon-thread probes.
"""
import os, asyncio, threading, time, unittest, logging, json, re
os.environ['DATABASE_URL']='';os.environ['BOT_TOKEN']=''
from unittest.mock import patch, AsyncMock, MagicMock
from pathlib import Path
from types import SimpleNamespace
import tests.test_phase16_admin_fail_order as f
from reseller_api.services import order_notifications as n
from reseller_api.services.reconciliation import reconcile_reseller
from database.pgcompat import atomic
import config
import web_admin.app as wa

db,rdb=f.db,f.rdb
_counter=0


class NotificationsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):db.init_db();f.Phase16AdminFailOrderTests.setUpClass()
    @classmethod
    def tearDownClass(cls):f.Phase16AdminFailOrderTests.tearDownClass()
    def setUp(self):
        global _counter
        _counter+=1;self.uid=99975000+_counter;db.ensure_user_id(self.uid)
        self.rs=rdb.create_reseller('Seller <b> & 🧩',self.uid,'notify-test');self.rid=self.rs['id'];self.fx=f.Phase16AdminFailOrderTests('test_successful_fail_full_effects');self.owner=self.fx._owner()
        self.jobs=[]
        def start(patcher):
            self.addCleanup(patcher.stop)
            return patcher.start()
        start(patch.object(config,'BOT_TOKEN','123456:UNIT_TEST_SYNTHETIC_NOTIFICATION_TOKEN'))
        start(patch.object(config,'ADMIN_ID',77775000))
        start(patch.object(n,'_start_job',side_effect=lambda job:self.jobs.append(job) or True))
        self.send=start(patch.object(n,'_send_once',return_value=True))
        db.set_setting('reseller_api_owner_notify','1')
    def drain(self):
        while self.jobs:self.jobs.pop(0)()
    def status(self,action):
        return self.owner.post(f'/api/resellers/{self.rs["public_id"]}/status',json={'action':action},headers={'X-CSRF-Token':'test-csrf'})
    def order(self):
        rs,oid,pid=self.fx._make_reseller_order();return rdb.get_reseller(rs['id']),oid,pid
    def scalar(self,sql,*params):db.cur.execute(sql,params);return db.cur.fetchone()[0]
    def cancel(self,oid):
        db.cur.execute("UPDATE orders SET delivery=NULL WHERE id=?",(oid,));db.conn.commit()
        return self.owner.post(f'/api/orders/{oid}/cancel',json={'reason':'test cancellation'},headers={'X-CSRF-Token':'test-csrf'})
    def test_all_status_transitions_send_expected_static_content_after_commit(self):
        for action,expected,phrase in [('approve','active','approved'),('suspend','suspended','support'),('reactivate','active','reactivated'),('block','closed','closed')]:
            before=self.send.call_count;result=self.status(action);self.assertEqual(result.status_code,200,result.get_json());self.assertEqual(rdb.get_reseller(self.rid)['status'],expected);self.assertFalse(db.get_conn().in_transaction);self.assertEqual(self.send.call_count,before)
            self.drain();self.assertEqual(self.send.call_count,before+1);chat,text=self.send.call_args.args;self.assertEqual(chat,self.uid);self.assertIn(phrase,text);self.assertIn(self.rs['public_id'],text)
        self.assertIn('Reseller Center',n.build_status_notification('approve',self.rs['public_id']));self.assertIn('Reseller Center',n.build_status_notification('reactivate',self.rs['public_id']))
    def test_failed_repeated_unknown_or_missing_status_no_notification(self):
        for action in ('suspend','bad'):
            self.assertEqual(self.status(action).status_code,400);self.assertEqual(self.jobs,[])
        self.assertEqual(self.status('approve').status_code,200);self.drain();self.assertEqual(self.status('approve').status_code,400);self.drain();self.assertEqual(self.send.call_count,1)
        response=self.owner.post('/api/resellers/RSR-AAAAAAAAAA/status',json={'action':'approve'},headers={'X-CSRF-Token':'test-csrf'});self.assertEqual(response.status_code,404);self.assertEqual(self.send.call_count,1)
    def test_status_notification_exception_never_rolls_back(self):
        self.send.side_effect=RuntimeError('SYNTHETIC-SEND-FAILURE')
        response=self.status('approve');self.assertEqual(response.status_code,200)
        # Job exception is caught by production _start_job wrapper. Test the route
        # remains successful even if the dispatcher itself raises unexpectedly.
        self.jobs.clear()
        with patch.object(n,'notify_reseller_status',side_effect=RuntimeError('SYNTHETIC-FAILURE')):response=self.status('suspend')
        self.assertEqual(response.status_code,200);self.assertEqual(rdb.get_reseller(self.rid)['status'],'suspended')
    def test_mock_bot_exception_status_commit_is_preserved(self):
        self.status('approve');job=self.jobs.pop(0)
        # Bypass the send spy with actual two-second wrapper and mocked Bot.
        with patch.object(n,'_send_once',side_effect=lambda chat,text:self.actual_send(chat,text)),patch('telegram.Bot') as bot:
            bot.return_value.__aenter__=AsyncMock(side_effect=RuntimeError('BOT-CREDENTIAL-NEVER-LOG'))
            with self.assertLogs(n.log,level='WARNING') as capture:job()
        self.assertEqual(rdb.get_reseller(self.rid)['status'],'active');self.assertNotIn('BOT-CREDENTIAL-NEVER-LOG',str(capture.output))
    def actual_send(self,chat,text):
        return _REAL_SEND(chat,text)
    def test_order_created_is_deferred_contains_public_information_and_no_pii(self):
        rs,oid,pid=self.order();self.assertEqual(self.send.call_count,0);self.assertFalse(db.get_conn().in_transaction);self.drain();self.assertEqual(self.send.call_count,1)
        chat,text=self.send.call_args.args;self.assertEqual(chat,config.ADMIN_ID)
        for phrase in (rs['public_id'],rs['name'],f'ORD-{oid:08d}','Quantity: <code>1</code>','10.00 USDT','P16F Product'):self.assertIn(phrase,text)
        for phrase in ('F-CODE-',str(rs['telegram_user_id']),'rk_test_','sk_test_','buyer_email','unit_price_cents'):self.assertNotIn(phrase,text)
    def test_telegram_failure_order_stays_201_and_financially_valid(self):
        self.send.return_value=False;rs,oid,pid=self.order();self.drain();self.assertEqual(rdb.get_wallet_balance_cents(rs['id']),49000);self.assertEqual(reconcile_reseller(rs['id']),[])
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM orders WHERE id=?',oid),1)
    def test_creation_dispatcher_exception_cannot_change_api_order_result(self):
        with patch.object(n,'queue_owner_order_event',side_effect=RuntimeError('SECRET-NOT-LOGGED')):
            with self.assertLogs('reseller_api.services.order_service',level='WARNING') as capture:rs,oid,pid=self.order()
        self.assertNotIn('SECRET-NOT-LOGGED',str(capture.output));self.assertEqual(reconcile_reseller(rs['id']),[])
    def test_owner_toggle_default_on_disable_silent_and_does_not_disable_status(self):
        db.cur.execute("DELETE FROM settings WHERE key='reseller_api_owner_notify'");db.conn.commit();self.assertEqual(self.owner.get('/api/settings').get_json()['settings']['reseller_api_owner_notify'],'1')
        self.order();db.set_setting('reseller_api_owner_notify','0')
        with patch.object(n.log,'info') as info,patch.object(n.log,'warning') as warn:self.drain();info.assert_not_called();warn.assert_not_called()
        self.send.assert_not_called();self.assertEqual(self.status('approve').status_code,200);self.drain();self.assertEqual(self.send.call_args.args[0],self.uid)
    def test_setting_validation_persistence_and_kill_switch_independence(self):
        response=self.owner.post('/api/settings/update',json={'reseller_api_owner_notify':'off'},headers={'X-CSRF-Token':'test-csrf'});self.assertEqual(response.status_code,200);self.assertEqual(db.get_setting('reseller_api_owner_notify'),'0')
        for value in ('garbage',{},2):
            response=self.owner.post('/api/settings/update',json={'reseller_api_owner_notify':value},headers={'X-CSRF-Token':'test-csrf'});self.assertEqual(response.status_code,400)
        db.set_setting('reseller_api_global_enabled','0');self.owner.post('/api/settings/update',json={'reseller_api_owner_notify':'on'},headers={'X-CSRF-Token':'test-csrf'});self.assertEqual(db.get_setting('reseller_api_global_enabled'),'0');self.assertEqual(db.get_setting('reseller_api_owner_notify'),'1')
        db.set_setting('reseller_api_global_enabled','1')
    def test_no_telegram_link_skips_with_sanitized_log(self):
        db.cur.execute('UPDATE resellers SET telegram_user_id=NULL WHERE id=?',(self.rid,));db.conn.commit()
        with self.assertLogs(n.log,level='INFO') as capture:self.assertEqual(self.status('approve').status_code,200)
        self.assertIn('no_telegram_link',str(capture.output));self.assertEqual(self.jobs,[]);self.send.assert_not_called()
    def test_owner_api_order_without_telegram_link_still_notifies_owner(self):
        rs,oid,pid=self.order();db.cur.execute('UPDATE resellers SET telegram_user_id=NULL WHERE id=?',(rs['id'],));db.conn.commit();self.drain();self.assertEqual(self.send.call_count,1);self.assertEqual(self.send.call_args.args[0],config.ADMIN_ID)
    def test_fail_cancel_routes_shared_messages_and_retry_no_duplicates(self):
        for event in ('fail','cancel'):
            rs,oid,pid=self.order();self.drain();self.send.reset_mock()
            result=self.fx._fail(self.owner,oid) if event=='fail' else self.cancel(oid)
            self.assertEqual(result.status_code,200,result.get_json());self.assertEqual(result.get_json()['notify'],'queued');self.drain();self.assertEqual(self.send.call_count,2)
            self.assertEqual({call.args[0] for call in self.send.call_args_list},{config.ADMIN_ID,rs['telegram_user_id']})
            repeated=self.fx._fail(self.owner,oid) if event=='fail' else self.cancel(oid);self.assertEqual(repeated.status_code,409);self.drain();self.assertEqual(self.send.call_count,2);self.assertEqual(reconcile_reseller(rs['id']),[])
    def test_owner_and_reseller_logical_event_claims_survive_repeated_dispatch(self):
        rs,oid,pid=self.order();self.drain();self.send.reset_mock()
        for _ in range(3):n.queue_owner_order_event(oid,'order.created')
        self.drain();self.send.assert_not_called()
        self.fx._fail(self.owner,oid);self.drain();self.send.reset_mock()
        for _ in range(3):n.notify_order_failed(rs['telegram_user_id'],oid,{'product_name':'same'},'retry');n.queue_owner_order_event(oid,'order.failed')
        self.drain();self.send.assert_not_called()
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM settings WHERE key=?',f'telegram.owner.order.failed.ORD-{oid:08d}'),1)
    def test_failed_send_claim_prevents_misleading_repeated_success_attempts(self):
        rs,oid,pid=self.order();self.send.return_value=False;self.drain();self.assertEqual(self.send.call_count,1)
        n.queue_owner_order_event(oid,'order.created');self.drain();self.assertEqual(self.send.call_count,1)
    def test_uncommitted_or_nonexistent_logical_events_never_send(self):
        rs,oid,pid=self.order();self.drain();self.send.reset_mock()
        n.queue_owner_order_event(oid,'order.failed');self.drain();self.send.assert_not_called()
        with atomic(db.get_conn()):
            with self.assertLogs(n.log,level='WARNING'):self.assertFalse(n.queue_owner_order_event(oid,'order.created'))
        self.assertEqual(self.jobs,[])
    def test_order_replay_and_failed_creation_no_extra_owner_job(self):
        rdb.set_reseller_status(self.rid,'active');key,secret=rdb.create_api_key(self.rid,'test');db.cur.execute("INSERT INTO products(name,price,stock,delivery,active) VALUES('Replay',1,10,'static',1)");pid=db.cur.lastrowid;db.conn.commit();pub=rdb.ensure_product_public_id(pid);rdb.set_product_access(self.rid,pid,True);rdb.import_reseller_product(self.rid,pid);rdb.post_wallet_transaction(self.rid,'topup_credit',1000,'notify-replay','seed')
        headers=f._hdr(key,secret);payload={'product_id':pub,'quantity':1,'client_order_id':'notification-replay'}
        c=f.Phase16AdminFailOrderTests.client
        self.assertEqual(c.post('/api/v1/orders',headers=headers,json=payload).status_code,201);self.drain();self.assertEqual(self.send.call_count,1)
        self.assertEqual(c.post('/api/v1/orders',headers=headers,json=payload).status_code,200);self.assertEqual(self.jobs,[])
        self.assertEqual(c.post('/api/v1/orders',headers=headers,json={**payload,'quantity':0,'client_order_id':'invalid'}).status_code,400);self.assertEqual(self.jobs,[])
    def test_html_length_unicode_pii_and_secret_scan_all_builders(self):
        sensitive='buyer@example.org rk_test_ABCDEFGH Bearer abc.def token=private-value '+config.BOT_TOKEN
        snapshot=dict(order_id='ORD-00000001',reseller_public_id=self.rs['public_id'],reseller_name='<&>🧩'*3000+sensitive,product_name=sensitive+'<&>🧩'*3000,quantity=2,total_cents=201,buyer_email='EXCLUDED-BUYER@example.org',delivery='EXCLUDED-DELIVERY',fulfillment_config={'api_key':'EXCLUDED-SUPPLIER'})
        texts=[n.build_owner_order_notification(event,snapshot) for event in n._EVENTS]
        for event in ('failed','cancelled'):
            captured=[]
            fn=n.notify_order_failed if event=='failed' else n.notify_order_cancelled
            fn(self.uid,1,{'product_name':sensitive+'<&>'*3000},sensitive+'<&>'*3000,100,send=lambda chat,text:(captured.append(text) or True,'ok'));texts+=captured
        for text in texts:
            self.assertLess(len(text),4096);self.assertIn('&lt;',text);self.assertNotIn('<&>',text)
            for secret in ('buyer@example.org','rk_test_ABCDEFGH','abc.def','private-value',config.BOT_TOKEN,'EXCLUDED-BUYER','EXCLUDED-DELIVERY','EXCLUDED-SUPPLIER'):self.assertNotIn(secret,text)
            # Only our own well-formed b/code tags remain; no cut entity/tag.
            stripped=re.sub(r'</?(?:b|code)>','',text);self.assertNotIn('<',stripped);self.assertNotRegex(stripped,r'&(?!amp;|lt;|gt;|quot;|#x27;)')
    def test_mock_bot_receives_html_once_no_real_key_or_delivery_fields(self):
        with patch('telegram.Bot') as Bot:
            bot=MagicMock();bot.send_message=AsyncMock();Bot.return_value.__aenter__=AsyncMock(return_value=bot);Bot.return_value.__aexit__=AsyncMock(return_value=False)
            self.assertTrue(self.actual_send(self.uid,n.build_status_notification('approve',self.rs['public_id'])))
            bot.send_message.assert_awaited_once();self.assertEqual(bot.send_message.await_args.kwargs['parse_mode'],'HTML');self.assertEqual(bot.send_message.await_args.kwargs['read_timeout'],2)
    def test_transport_timeout_budget_and_sanitized_adversarial_logs(self):
        async def slow(chat,text):
            logging.getLogger('httpx').error('SECRET-HTTP-URL '+config.BOT_TOKEN+' buyer@example.org')
            await asyncio.sleep(10)
        started=time.monotonic()
        with patch.object(n,'_bot_send',side_effect=slow),patch.object(n,'NOTIFICATION_BUDGET_SECONDS',0.04),self.assertLogs(level='INFO') as captured:
            self.assertFalse(self.actual_send(self.uid,'safe'))
        self.assertLess(time.monotonic()-started,0.5);self.assertNotIn(config.BOT_TOKEN,str(captured.output));self.assertNotIn('buyer@example.org',str(captured.output));self.assertIn('telegram_unavailable',str(captured.output))
        self.assertLessEqual(n.NOTIFICATION_BUDGET_SECONDS,2)
    def test_unrelated_logging_remains_unchanged(self):
        n._guard_transport_logging()
        with self.assertLogs('normal-test',level='INFO') as captured:logging.getLogger('normal-test').info('Ordinary message %s','unchanged')
        self.assertIn('Ordinary message unchanged',str(captured.output))
    def test_thread_capacity_and_dispatch_failure_nonblocking(self):
        # Invoke saved production scheduler rather than the deterministic queue spy.
        with patch.object(n,'_MAX_JOBS',threading.BoundedSemaphore(1)),patch('threading.Thread') as Thread:
            Thread.return_value.start.side_effect=RuntimeError('SECRET-THREAD')
            with self.assertLogs(n.log,level='WARNING') as captured:self.assertFalse(_REAL_START(lambda:None))
            self.assertNotIn('SECRET-THREAD',str(captured.output));self.assertTrue(n._MAX_JOBS.acquire(False));n._MAX_JOBS.release()
        held=threading.BoundedSemaphore(1);held.acquire()
        with patch.object(n,'_MAX_JOBS',held),self.assertLogs(n.log,level='WARNING'):self.assertFalse(_REAL_START(lambda:None))
    def test_actual_daemon_send_does_not_block_order_201(self):
        entered=threading.Event();release=threading.Event();finished=threading.Event()
        def slow_send(chat,text):entered.set();release.wait(1);return False
        def runner(job):
            def wrapped():
                try:job()
                finally:finished.set()
            return _REAL_START(wrapped)
        with patch.object(n,'_start_job',side_effect=runner),patch.object(n,'_send_once',side_effect=slow_send):
            try:
                started=time.monotonic();rs,oid,pid=self.order();elapsed=time.monotonic()-started;self.assertTrue(entered.wait(1));self.assertLess(elapsed,0.7);self.assertFalse(finished.is_set());self.assertEqual(rdb.get_wallet_balance_cents(rs['id']),49000)
            finally:release.set();self.assertTrue(finished.wait(2))
    def test_actual_daemon_status_send_is_nonblocking(self):
        entered=threading.Event();release=threading.Event();finished=threading.Event()
        def slow_send(chat,text):entered.set();release.wait(1);finished.set();return False
        with patch.object(n,'_start_job',side_effect=_REAL_START),patch.object(n,'_send_once',side_effect=slow_send):
            try:
                started=time.monotonic();response=self.status('approve');self.assertEqual(response.status_code,200);self.assertLess(time.monotonic()-started,0.7);self.assertTrue(entered.wait(1));self.assertFalse(finished.is_set());self.assertEqual(rdb.get_reseller(self.rid)['status'],'active')
            finally:release.set();self.assertTrue(finished.wait(2))
    def test_mock_bot_timeout_after_actual_201_preserves_wallet(self):
        rs,oid,pid=self.order()
        async def timeout(chat,text):await asyncio.sleep(10)
        with patch.object(n,'_send_once',side_effect=lambda chat,text:self.actual_send(chat,text)),patch.object(n,'_bot_send',side_effect=timeout),patch.object(n,'NOTIFICATION_BUDGET_SECONDS',0.03),self.assertLogs(n.log,level='WARNING'):
            self.drain()
        self.assertEqual(rdb.get_wallet_balance_cents(rs['id']),49000);self.assertEqual(reconcile_reseller(rs['id']),[])
    def test_owner_disabled_fail_alert_does_not_disable_reseller_notice(self):
        rs,oid,pid=self.order();self.drain();self.send.reset_mock();db.set_setting('reseller_api_owner_notify','0')
        response=self.fx._fail(self.owner,oid);self.assertEqual(response.status_code,200);self.drain()
        self.assertEqual(self.send.call_count,1);self.assertEqual(self.send.call_args.args[0],rs['telegram_user_id'])
    def test_incomplete_financial_action_has_no_success_alert_until_recovered(self):
        rs,oid,pid=self.order();self.drain();self.send.reset_mock()
        with patch('reseller_api.services.order_failure._post_refund_serialized',side_effect=RuntimeError('TEST-ROLLBACK')):
            response=self.fx._fail(self.owner,oid)
        self.assertEqual(response.status_code,500);self.assertEqual(self.jobs,[]);self.send.assert_not_called()
        response=self.fx._fail(self.owner,oid);self.assertEqual(response.status_code,200);self.drain();self.assertEqual(self.send.call_count,2);self.assertEqual(reconcile_reseller(rs['id']),[])
    def test_concurrent_status_cas_only_one_transition_wins(self):
        barrier=threading.Barrier(2);results=[];lock=threading.Lock();original=rdb.get_reseller
        def read(rid):
            row=original(rid)
            if row and row['status']=='pending':barrier.wait(2)
            return row
        def run():
            try:
                result=rdb.apply_reseller_action(self.rid,'approve')
                with lock:results.append(result)
            finally:db.close_thread_connection()
        with patch.object(rdb,'get_reseller',side_effect=read):
            threads=[threading.Thread(target=run) for _ in range(2)]
            for t in threads:t.start()
            for t in threads:t.join(4)
        self.assertEqual(sum(ok for ok,_,_ in results),1);self.assertEqual(len(results),2);self.assertEqual(original(self.rid)['status'],'active')
    def test_concurrent_order_alert_claim_only_one_attempt(self):
        rs,oid,pid=self.order();self.jobs.clear();barrier=threading.Barrier(2);results=[]
        def run():
            try:barrier.wait(2);results.append(n._claim_order_event(oid,'order.created','owner'))
            finally:db.close_thread_connection()
        threads=[threading.Thread(target=run) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(4)
        self.assertEqual(sorted(results),[False,True])
    def test_customer_notification_source_and_kill_switch_untouched(self):
        source=(Path(__file__).resolve().parents[1]/'products/product_handlers.py').read_text();self.assertIn('async def notify_admin_order(',source);self.assertIn('context_bot.send_message',source)
        script=(Path(__file__).resolve().parents[1]/'web_admin/static/js/app.js').read_text();self.assertIn('set_reseller_api_owner_notify',script);self.assertIn('set_reseller_api_global_enabled',script)

_REAL_SEND=n._send_once
_REAL_START=n._start_job
if __name__=='__main__':unittest.main()
