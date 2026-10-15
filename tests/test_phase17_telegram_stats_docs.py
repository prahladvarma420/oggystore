"""17.4: real API parity, registered-route proof, read-only and privacy guards.

Temporary SQLite; production aggregates/routes/auth remain real. Infrastructure
rate/audit persistence is isolated by the established fixture; seed audit rows
explicitly so measurement requests do not alter the parity denominator.
"""
import os, asyncio, re, unittest, ast
os.environ['DATABASE_URL']='';os.environ['BOT_TOKEN']=''
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace
from pathlib import Path
import tests.test_phase16_admin_fail_order as fixtures
from handlers import reseller_handlers as h
import config
from reseller_api.services.rate_limit import RequestRateDecision
from reseller_api.services.endpoint_rate_limit import EndpointRateDecision
from utils.timezone import now_text

db,rdb=fixtures.db,fixtures.rdb
_counter=0


def documented_operations(resources):
    return {(method,path) for category,ops in resources for method,path in ops}


def public_registered_operations(app):
    result=set()
    for rule in app.url_map.iter_rules():
        if not rule.endpoint.startswith('reseller_api_v1.') or not rule.rule.startswith('/api/v1/'):
            continue
        normalized=re.sub(r'<(?:[^:<>]+:)?([^<>]+)>',r'{\1}',rule.rule)
        result.update((method,normalized) for method in rule.methods-{'OPTIONS','HEAD'})
    return result


def assert_documented_registered(case,resources,registered):
    case.assertTrue(documented_operations(resources)<=registered,
                    str(documented_operations(resources)-registered))


class TelegramStatsDocsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):db.init_db();fixtures.Phase16AdminFailOrderTests.setUpClass()
    @classmethod
    def tearDownClass(cls):fixtures.Phase16AdminFailOrderTests.tearDownClass()
    def setUp(self):
        global _counter
        _counter+=1;self.uid=99974000+_counter;db.ensure_user_id(self.uid)
        self.rs=rdb.create_reseller('Statistics <private> & name',self.uid,'stats-test');self.rid=self.rs['id'];rdb.set_reseller_status(self.rid,'active')
        self.key,self.secret=rdb.create_api_key(self.rid,'test');self.headers=fixtures._hdr(self.key,self.secret);self.api=fixtures.Phase16AdminFailOrderTests.client
    def endpoint(self,url):
        response=self.api.get(url,headers=self.headers);self.assertEqual(response.status_code,200,response.get_json());return response.get_json()['data']
    def callback(self,data):
        q=SimpleNamespace(answer=AsyncMock())
        with patch.object(h,'edit_or_send',new=AsyncMock()) as edit:
            self.assertTrue(asyncio.run(h.handle_reseller_callback(q,self.uid,data,None)))
            return edit.await_args.args[1],edit.await_args.args[2]
    def seed_activity(self):
        rdb.post_wallet_transaction(self.rid,'topup_credit',10000,f'stats-seed-{self.uid}','seed')
        db.cur.execute("INSERT INTO products(name,price,stock,delivery,active) VALUES('STATS-PRIVATE-PRODUCT',3.21,4,'manual',1)")
        pid=db.cur.lastrowid;db.cur.execute('UPDATE products SET max_per_order=10 WHERE id=?',(pid,));db.conn.commit();pub=rdb.ensure_product_public_id(pid)
        rdb.set_product_access(self.rid,pid,True);rdb.import_reseller_product(self.rid,pid)
        response=self.api.post('/api/v1/orders',headers=self.headers,json={'product_id':pub,'quantity':2,'client_order_id':f'stats-{self.uid}'})
        self.assertEqual(response.status_code,201,response.get_json());oid=int(response.get_json()['data']['order_id'][4:])
        for status in (200,200,429):rdb.log_api_request(self.rid,None,'PRIVATE-ENDPOINT','GET',status,ip='SENSITIVE-IP',user_agent='SENSITIVE-UA')
        rdb.set_quota_settings(self.rid,daily_order_quota=7,monthly_order_quota=20)
        for i in range(5):
            db.cur.execute('INSERT INTO reseller_webhooks(reseller_id,url,secret_encrypted,enabled,created,updated) VALUES(?,?,?,1,?,?)',(self.rid,'https://private-endpoint.invalid/'+str(i)+'x'*1000,'SECRET-WEBHOOK',now_text(),now_text()))
        db.conn.commit();return oid,pid
    def test_statistics_semantic_parity_with_real_api_and_wallet_webhooks(self):
        self.seed_activity();m=h.read_reseller_telegram_statistics(self.uid)
        usage=self.endpoint('/api/v1/usage')['usage'];sales=self.endpoint('/api/v1/analytics/sales')['sales'];wallet=self.endpoint('/api/v1/wallet/summary')['wallet'];wh=self.endpoint('/api/v1/webhooks/analytics')['webhooks']
        for source,key in [('requests_today','today'),('requests_total','total')]:self.assertEqual(m['usage'][source],usage['requests'][key])
        for source,key in [('orders_today','today'),('orders_this_month','this_month')]:self.assertEqual(m['usage'][source],usage['orders'][key])
        for source,key in [('quota_daily','daily_limit'),('quota_monthly','monthly_limit'),('quota_daily_remaining','daily_remaining'),('quota_monthly_remaining','monthly_remaining')]:self.assertEqual(m['usage'][source],usage['quota'][key])
        for key in m['sales']:self.assertEqual(m['sales'][key],sales[key])
        self.assertEqual(m['wallet']['balance'],wallet['balance']);self.assertEqual(m['wallet']['balance'],'93.58')
        self.assertEqual(m['webhooks'],{k:wh[k] for k in m['webhooks']})
        text,_=h.build_reseller_statistics(m)
        for value in ('Today: 3','Total: 3','Orders today: 1','Orders this month: 1','remaining 6','remaining 19','Gross sales: 6.42 USDT','Net sales: 6.42 USDT','Balance: 93.58 USDT','Configured: 5'):self.assertIn(value,text)
    def test_zero_activity_and_unlimited_quota_are_honest(self):
        m=h.read_reseller_telegram_statistics(self.uid);text,_=h.build_reseller_statistics(m)
        for phrase in ('Today: 0','Total: 0','Orders today: 0','Orders this month: 0','Gross sales: 0.00 USDT','Balance: 0.00 USDT','remaining Unlimited','No webhooks configured.'):self.assertIn(phrase,text)
        for phrase in ('None','NaN','Healthy','successful delivery'):self.assertNotIn(phrase,text)
    def test_usage_and_sales_preserve_different_existing_boundaries(self):
        self.seed_activity()
        # Existing usage today/month buckets are shop-prefix comparisons; sales
        # uses its own default rolling UTC order window + shop refund window.
        db.cur.execute("UPDATE reseller_api_logs SET created='2000-01-01 00:00:00' WHERE reseller_id=?",(self.rid,));db.cur.execute("UPDATE orders SET created='2000-01-01 00:00:00' WHERE reseller_id=?",(self.rid,));db.conn.commit()
        m=h.read_reseller_telegram_statistics(self.uid);u=self.endpoint('/api/v1/usage')['usage'];sales=self.endpoint('/api/v1/analytics/sales')['sales']
        self.assertEqual(m['usage']['requests_total'],3);self.assertEqual(m['usage']['requests_today'],u['requests']['today']);self.assertEqual(m['usage']['orders_today'],u['orders']['today']);self.assertEqual(m['sales']['gross_sales'],sales['gross_sales']);self.assertEqual(m['sales']['gross_sales'],'0.00')
    def test_wallet_refunds_net_sales_match_authoritative_services(self):
        oid,_=self.seed_activity()
        from reseller_api.services.order_failure import handle_cancellation
        handle_cancellation(oid,'statistics test',refund_if_debited=True)
        m=h.read_reseller_telegram_statistics(self.uid);sales=self.endpoint('/api/v1/analytics/sales')['sales']
        self.assertEqual(m['sales']['net_sales'],sales['net_sales']);self.assertEqual(m['sales']['net_sales'],'0.00');self.assertEqual(m['wallet']['balance'],'100.00')
    def test_webhook_real_pending_delivered_dead_status_summary(self):
        self.seed_activity()
        # Actual creation produces pending outbox; adjust persisted test rows
        # to exercise the analytics buckets without making external requests.
        db.cur.execute("UPDATE webhook_outbox SET status='failed' WHERE reseller_id=? AND event_type='delivery.completed'",(self.rid,));db.conn.commit()
        m=h.read_reseller_telegram_statistics(self.uid);wh=self.endpoint('/api/v1/webhooks/analytics')['webhooks'];self.assertEqual(m['webhooks']['pending'],wh['pending']);self.assertEqual(m['webhooks']['dead_lettered'],wh['dead_lettered'])
        text,_=h.build_reseller_statistics(m);self.assertIn(f"dead-lettered: {wh['dead_lettered']}",text);self.assertNotIn('Healthy',text)
    def test_no_activity_configured_webhooks_not_misleading_success(self):
        for i in range(5):db.cur.execute('INSERT INTO reseller_webhooks(reseller_id,url,secret_encrypted,enabled,created,updated) VALUES(?,?,?,1,?,?)',(self.rid,'https://private.invalid/'+str(i),'encrypted',now_text(),now_text()))
        db.conn.commit();self.assertIn('health not yet established',self.callback('res_stats')[0])
    def test_large_fixtures_messages_bounded_no_sensitive_fields(self):
        self.seed_activity();text,_=self.callback('res_stats');self.assertLess(len(text),4096)
        for phrase in ('PRIVATE-ENDPOINT','SENSITIVE-IP','SENSITIVE-UA','SECRET-WEBHOOK','private-endpoint','STATS-PRIVATE-PRODUCT',self.secret,self.key,'top_products','price_source'):self.assertNotIn(phrase,text)
        # Massive unrelated source dictionaries cannot expand the summary.
        m=h.read_reseller_telegram_statistics(self.uid);m['webhooks']['payload']='SENSITIVE'*10000;m['sales']['top_products']=['x'*10000]*1000
        self.assertLess(len(h.build_reseller_statistics(m)[0]),4096)
    def test_cross_reseller_usage_wallet_sales_never_combined(self):
        self.seed_activity();other_uid=self.uid+100000;db.ensure_user_id(other_uid);other=rdb.create_reseller('FOREIGN',other_uid,'foreign');rdb.set_reseller_status(other['id'],'active');rdb.post_wallet_transaction(other['id'],'topup_credit',999999,'foreign-stats','foreign')
        for _ in range(10):rdb.log_api_request(other['id'],None,'foreign','GET',200)
        m=h.read_reseller_telegram_statistics(self.uid);self.assertEqual(m['usage']['requests_total'],3);self.assertEqual(m['wallet']['balance'],'93.58')
    def test_inactive_blocked_and_unlinked_authorization_every_view(self):
        for status in ('pending','suspended','closed'):
            rdb.set_reseller_status(self.rid,status)
            for action in ('res_stats','res_api_docs','res_api_docs:page:2'):self.assertIn('Active reseller access required',self.callback(action)[0])
            with self.assertRaises(PermissionError):h.read_reseller_telegram_statistics(self.uid)
        rdb.set_reseller_status(self.rid,'active');db.cur.execute('UPDATE users SET blocked=1 WHERE user_id=?',(self.uid,));db.conn.commit()
        self.assertIn('Active reseller access required',self.callback('res_stats')[0])
        with self.assertRaises(PermissionError):h.read_reseller_telegram_statistics(self.uid+999999)
    def test_authorization_revalidated_between_reads_and_refreshes(self):
        self.callback('res_stats');rdb.set_reseller_status(self.rid,'suspended');self.assertIn('Active reseller access required',self.callback('res_stats')[0]);self.assertIn('Active reseller access required',self.callback('res_api_docs:page:4')[0])
        rdb.set_reseller_status(self.rid,'active')
        with patch.object(h,'get_active_reseller',side_effect=[self.rs,None]):
            with self.assertRaises(PermissionError):h.read_reseller_telegram_statistics(self.uid)
    def test_unavailable_service_does_not_fabricate_zero_or_leak_exception(self):
        with patch.object(rdb,'get_reseller_api_usage',side_effect=RuntimeError('SECRET-DB-PASSWORD')),self.assertLogs('oggystore_bot',level='ERROR') as captured:
            m=h.read_reseller_telegram_statistics(self.uid)
        text,_=h.build_reseller_statistics(m);self.assertIn('Today: Unavailable',text);self.assertNotIn('SECRET-DB-PASSWORD',text+str(captured.output));self.assertIn('Daily quota: Unavailable',text)
    def test_missing_quota_fields_unavailable_not_falsely_unlimited(self):
        with patch.object(rdb,'get_reseller_api_usage',return_value={'requests_today':0,'requests_total':0}):
            m=h.read_reseller_telegram_statistics(self.uid)
        text,_=h.build_reseller_statistics(m);self.assertIn('Daily quota: Unavailable',text);self.assertIn('Monthly quota: Unavailable',text);self.assertNotIn('Unlimited',text)
    def test_invalid_numeric_missing_values_not_none_nan_or_negative_quota(self):
        m={'usage':{'requests_today':'NaN','requests_total':float('inf'),'orders_today':-1,'quota_daily':2,'quota_daily_remaining':-3},'wallet':{'balance':'<secret>','currency':'<script>'},'sales':None,'webhooks':None}
        text,_=h.build_reseller_statistics(m)
        for phrase in ('NaN','inf','None','-3','<script>','<secret>'):self.assertNotIn(phrase,text)
        self.assertIn('remaining Unavailable',text)
    def test_statistics_builders_pure_and_read_only_no_id_self_healing(self):
        oid,pid=self.seed_activity();db.cur.execute('UPDATE products SET public_id=NULL WHERE id=?',(pid,));db.conn.commit();before=db.get_conn().total_changes
        with patch.object(rdb,'ensure_product_public_id',side_effect=AssertionError('forbidden read write')):
            metrics=h.read_reseller_telegram_statistics(self.uid);text,_=h.build_reseller_statistics(metrics)
        self.assertEqual(db.get_conn().total_changes,before);self.assertIn('6.42 USDT',text)
        with patch.object(rdb,'get_cursor',side_effect=AssertionError('hidden builder read')),patch.object(h,'get_active_reseller',side_effect=AssertionError('hidden builder auth')):
            self.assertEqual(h.build_reseller_statistics(metrics),h.build_reseller_statistics(metrics));h.build_reseller_api_docs(1,'https://shop.example.org/api/v1')
    def test_api_sales_default_breakdowns_and_response_contract_preserved(self):
        self.seed_activity();full=rdb.get_reseller_sales_analytics(self.rid);summary=rdb.get_reseller_sales_analytics(self.rid,include_breakdowns=False)
        for key,value in summary.items():self.assertEqual(full[key],value)
        self.assertIn('top_products',self.endpoint('/api/v1/analytics/sales')['sales']);self.assertIn('orders_by_date',full)
    def test_every_advertised_operation_matches_registered_v1_routes(self):
        registered=public_registered_operations(fixtures.Phase16AdminFailOrderTests.app);assert_documented_registered(self,h.API_DOC_RESOURCES,registered)
        self.assertEqual({name for name,_ in h.API_DOC_RESOURCES},{'Products','My products','Pricing','Orders','Quote','Delivery','Balance','Transactions','Refunds','Invoices','Statements','Usage','Analytics','Exports','Webhooks'})
        text='\n'.join(h.build_reseller_api_docs(page)[0] for page in (2,3))
        for method,path in documented_operations(h.API_DOC_RESOURCES):self.assertIn(f'{method} {path}',text)
    def test_route_coverage_detects_removed_renamed_wrong_method_and_private(self):
        registered=public_registered_operations(fixtures.Phase16AdminFailOrderTests.app)
        for pair in (('GET','/api/v1/usage'),('POST','/api/v1/orders/quote')):
            with self.assertRaises(AssertionError):assert_documented_registered(self,h.API_DOC_RESOURCES,registered-{pair})
        for pair in (('GET','/api/v1/nonexistent'),('DELETE','/api/v1/usage'),('GET','/api/orders/{oid}/fail'),('GET','/health')):
            with self.assertRaises(AssertionError):assert_documented_registered(self,(('Bogus',(pair,)),),registered)
    def test_base_url_configured_and_missing_no_inferred_host(self):
        with patch.object(config,'RESELLER_API_PUBLIC_BASE_URL','https://public-shop.example.org/api/v1'):
            text,_=self.callback('res_api_docs');self.assertIn('https://public-shop.example.org/api/v1',text)
        with patch.object(config,'RESELLER_API_PUBLIC_BASE_URL',''):
            text,_=self.callback('res_api_docs');self.assertIn('Public base URL unavailable',text);self.assertNotIn('https://',text)
    def test_unsafe_base_urls_not_echoed(self):
        for raw in ('http://shop.example.org/api/v1','https://localhost/api/v1','https://127.0.0.1/api/v1','https://10.0.0.1/api/v1','https://[::1]/api/v1','https://svc.railway.internal/api/v1','https://user:SECRET@shop.example.org/api/v1','https://shop.example.org/api/v1?secret=hidden','https://shop.example.org/internal','https://2130706433/api/v1','https://127.1/api/v1','https://0x7f000001/api/v1','https://a.test/api/v1','https://shop.example.org/api/v1#secret'):
            self.assertIsNone(h.public_api_docs_base_url(raw));text,_=h.build_reseller_api_docs(1,raw);self.assertIn('unavailable',text.lower());self.assertNotIn(raw,text);self.assertNotIn('SECRET',text)
    def test_documentation_accuracy_auth_environment_replay_and_reference(self):
        pages=[h.build_reseller_api_docs(n)[0] for n in range(1,5)];text='\n'.join(pages)
        for phrase in ('Authorization: Bearer &lt;key_id&gt;.&lt;secret&gt;','test (sandbox) and production','does NOT automatically isolate external suppliers','HTTP 200 with idempotent: true','even if order parameters differ','per reseller account','RateLimit-Limit','RateLimit-Remaining','RateLimit-Reset','Retry-After','RateLimit-Category','request_id','RESELLER_API.md','request the current copy from the owner','buyer email'):self.assertIn(phrase,text)
        self.assertTrue((Path(__file__).resolve().parents[1]/'RESELLER_API.md').is_file())
        for forbidden in ('Coming soon','No public reseller API endpoints','/health/db','web_admin/app.py','/data/',self.secret,'price_source','rk_test_'):self.assertNotIn(forbidden,text)
    def test_documented_error_fields_and_rate_headers_match_live_responses(self):
        response=self.api.get('/api/v1/orders/ORD-99999999',headers=self.headers);self.assertEqual(response.status_code,404);body=response.get_json();self.assertEqual(set(body),{'ok','error','request_id'});self.assertEqual(set(body['error']),{'code','message'})
        response=self.api.get('/api/v1/usage',headers=self.headers)
        for field in RequestRateDecision(True,'allowed').headers():self.assertIn(field,response.headers);self.assertIn(field,h.build_reseller_api_docs(4)[0])
        headers=EndpointRateDecision(False,'read',1,0,20).headers(include_retry=True)
        for field in headers:self.assertIn(field,h.build_reseller_api_docs(4)[0])
    def test_actual_global_and_category_rate_denial_headers(self):
        with patch('reseller_api.middleware.rate_limit.evaluate_request_rate',return_value=RequestRateDecision(False,'rate_limit_exceeded',limit=1,remaining=0,reset_after=20)):
            response=self.api.get('/api/v1/usage',headers=self.headers)
        self.assertEqual(response.status_code,429);self.assertEqual(response.headers['Retry-After'],'20')
        with patch('reseller_api.middleware.endpoint_rate_limit.check_endpoint_rate',return_value=EndpointRateDecision(False,'read',1,0,20)):
            response=self.api.get('/api/v1/usage',headers=self.headers)
        self.assertEqual(response.status_code,429);self.assertEqual(response.headers['RateLimit-Category'],'read');self.assertIn('Retry-After',response.headers)
        for name in ('RateLimit-Limit','RateLimit-Remaining','RateLimit-Reset','RateLimit-Category','Retry-After'):self.assertIn(name,h.build_reseller_api_docs(4)[0])
    def test_authorization_read_errors_are_sanitized(self):
        with patch.object(h,'get_active_reseller',side_effect=RuntimeError('SECRET-AUTH-CONNECTION')),self.assertLogs('oggystore_bot',level='ERROR') as captured:
            text,_=self.callback('res_stats')
        self.assertIn('temporarily unavailable',text);self.assertNotIn('SECRET-AUTH-CONNECTION',text+str(captured.output))
    def test_every_docs_page_bounded_html_and_navigation_callbacks(self):
        for page in range(1,5):
            text,markup=h.build_reseller_api_docs(page,'https://shop.example.org/api/v1');self.assertLess(len(text),4096)
            for row in markup.inline_keyboard:
                for button in row:
                    if button.callback_data.startswith('res_api_docs'):self.assertRegex(button.callback_data,r'^res_api_docs:page:[1-4]$')
            self.assertIn(f'{page}/4',self.callback(f'res_api_docs:page:{page}')[0])
    def test_docs_page_callback_malformed_safe_and_out_of_range_clamped(self):
        for value in ('abc','1.0','+1','9'*1000,'','1:reseller:999'):
            self.assertIn('Invalid documentation link',self.callback('res_api_docs:page:'+value)[0])
        self.assertIn('1/4',self.callback('res_api_docs:page:-1')[0]);self.assertIn('4/4',self.callback('res_api_docs:page:9999999999')[0])
    def test_settings_and_all_reseller_soon_stubs_removed_navigation_preserved(self):
        text,markup=h.build_reseller_home(self.uid);callbacks=[b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertNotIn('res_soon',callbacks);self.assertNotIn('Settings',[b.text for row in markup.inline_keyboard for b in row]);self.assertIn('res_stats',callbacks)
        for action in ('res_wallet','res_keys','res_webhooks','res_api_docs','res_products:page:1','res_pricing:page:1'):self.assertIn(action,callbacks)
        self.assertFalse(asyncio.run(h.handle_reseller_callback(SimpleNamespace(),self.uid,'res_soon',None)))
        for path in ('handlers/reseller_handlers.py','keyboards/menus.py'):self.assertNotIn('res_soon',(Path(__file__).resolve().parents[1]/path).read_text())
    def test_refresh_reads_current_usage_quota_and_wallet_no_cache(self):
        first=self.callback('res_stats')[0];rdb.post_wallet_transaction(self.rid,'topup_credit',111,f'current-{self.uid}','seed');rdb.log_api_request(self.rid,None,'GET','GET',200);rdb.set_quota_settings(self.rid,daily_order_quota=5)
        second=self.callback('res_stats')[0];self.assertIn('Total: 0',first);self.assertIn('Total: 1',second);self.assertIn('Balance: 1.11 USDT',second);self.assertIn('remaining 5',second)
    def test_revoked_key_does_not_disable_active_telegram_identity(self):
        rdb.revoke_api_key_for_reseller(self.rid,self.key);self.assertIn('Statistics',self.callback('res_stats')[0]);self.assertIn('Reseller API v1',self.callback('res_api_docs')[0]);self.assertEqual(self.api.get('/api/v1/usage',headers=self.headers).status_code,401)

if __name__=='__main__':unittest.main()
