"""17.8 tooling tests; NOT evidence of real PostgreSQL/Railway performance.
Safety, percentiles, 200-way dispatch, evidence tampering, and a tiny real
SQLite service seed are tested without staging credentials or external HTTP.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import staging_load_common as common
import seed_staging_load as seed
import load_test as load
import verify_staging_load as verify

class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'operator.json'
        self.cfg = {'environment': 'test', 'dedicated_test_database': True,
                    'base_url': 'https://stage.example.invalid/api/v1',
                    'confirmed_test_host': 'stage.example.invalid',
                    'database_url': 'postgresql://unused', 'confirmed_database_name': 'disposable'}
        self.env = patch.dict(os.environ, {'RESELLER_API_ENVIRONMENT': 'test'})
        self.env.start()
        self.addCleanup(self.env.stop)
    def save(self):
        self.path.write_text(json.dumps(self.cfg))
        self.path.chmod(0o600)
    def test_private_config_accepts_explicit_test_confirmation(self):
        self.save()
        self.assertEqual(common.private_config(self.path)['environment'], 'test')
    def test_environment_confirmation_required(self):
        for key, value in [('environment', 'production'), ('dedicated_test_database', False), ('confirmed_test_host', 'other.invalid')]:
            with self.subTest(key=key):
                original = self.cfg[key]
                self.cfg[key] = value
                self.save()
                with self.assertRaises(common.SafetyError): common.private_config(self.path)
                self.cfg[key] = original
    def test_local_environment_not_sufficient(self):
        self.save()
        with patch.dict(os.environ, {'RESELLER_API_ENVIRONMENT': 'production'}):
            with self.assertRaises(common.SafetyError): common.private_config(self.path)
    def test_unsafe_urls_rejected(self):
        for url in ('http://stage.example.invalid/api/v1', 'https://fixture:example@stage.example.invalid/api/v1',
                    'https://stage.example.invalid/api/v1?token=private', 'https://localhost/api/v1',
                    'https://stage.example.invalid/private'):
            with self.subTest(url=url):
                self.cfg['base_url'] = url
                self.save()
                with self.assertRaises(common.SafetyError): common.private_config(self.path)
    @unittest.skipIf(os.name == 'nt', 'POSIX file-mode safeguard')
    def test_config_permissions_rejected(self):
        self.save()
        self.path.chmod(0o644)
        with self.assertRaises(common.SafetyError): common.private_config(self.path)
    def test_namespace_and_path_safety(self):
        for value in ('', '../oops', '%', 'x_y', 'a' * 33):
            with self.assertRaises(common.SafetyError): common.identifier(value)
        with self.assertRaises(common.SafetyError): common.validate_report_path(ROOT / 'bad.json')
    def test_guard_rejects_wrong_database_marker_or_live_credentials(self):
        for name, marker, live in [('wrong', 'test', 0), ('disposable', None, 0),
                                   ('disposable', 'production', 0), ('disposable', 'test', 1)]:
            conn = MagicMock()
            conn.execute.return_value.fetchone.side_effect = [{'name': name},
                {'value': marker} if marker else None, {'n': live}]
            with patch('psycopg.connect', return_value=conn):
                with self.assertRaises(common.SafetyError): common.guarded_database(self.cfg)
            conn.close.assert_called_once()
    def test_guard_validates_before_application_import(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = [{'name': 'disposable'}, {'value': 'test'}, {'n': 0}]
        with patch('psycopg.connect', return_value=conn) as connect:
            self.assertIs(common.guarded_database(self.cfg), conn)
        self.assertTrue(connect.call_args.kwargs['autocommit'])
    def test_raw_exception_text_never_printed(self):
        def fail(): raise RuntimeError('DO-NOT-LEAK-PASSWORD')
        out = io.StringIO()
        with contextlib.redirect_stderr(out): self.assertEqual(common.safe_main(fail), 1)
        self.assertNotIn('DO-NOT-LEAK', out.getvalue())
    def test_report_written_outside_source_and_private(self):
        output = Path(self.tmp.name) / 'result.json'
        common.write_report(output, {'status': 'NOT_RUN'})
        self.assertEqual(json.loads(output.read_text())['status'], 'NOT_RUN')
        if os.name != 'nt': self.assertEqual(output.stat().st_mode & 0o077, 0)
    def test_seed_dry_run_never_imports_application_or_writes_database(self):
        self.save()
        guard=MagicMock()
        guard.__enter__.return_value=guard
        output=Path(self.tmp.name)/'dry.json'
        args=['seed_staging_load.py','--config',str(self.path),'--namespace','dry-test','--report',str(output)]
        with patch.object(sys,'argv',args), patch.object(seed,'guarded_database',return_value=guard), patch.object(seed,'application_environment') as app:
            self.assertIsNone(seed.main())
        app.assert_not_called()
        guard.execute.assert_not_called()
        self.assertEqual(json.loads(output.read_text())['status'],'DRY_RUN')

    def test_seed_default_minimum_and_no_destructive_migration(self):
        self.assertEqual(seed.seed_plan(), {'orders': 50000, 'stock': 100000, 'resellers': 50, 'orders_per_reseller_max': 1000})
        for args in ((49999,100000,50), (50000,99999,50), (50000,100000,49)):
            with self.assertRaises(common.SafetyError): seed.seed_plan(*args)
        source = (ROOT / 'tools/seed_staging_load.py').read_text()
        self.assertNotIn('import migrate_sqlite_to_postgres', source)
        self.assertNotIn('DROP TABLE', source)
        self.assertNotIn('TRUNCATE ', source)
        self.assertIn('post_wallet_transaction', source)
        self.assertIn('create_reseller_order', source)
        self.assertIn('pg_try_advisory_lock', source)

class MetricsAndDispatchTests(unittest.TestCase):
    def test_percentiles_and_empty_results(self):
        self.assertIsNone(load.percentile([], .95))
        self.assertEqual(load.percentile([10], .99), 10)
        self.assertEqual(load.percentile([1,2,3,4], .5), 2.5)
        self.assertEqual(load.percentile([1,2,3,4], .95), 3.85)
        self.assertEqual(load.percentile([1,2,3,4], .99), 3.97)
    def test_errors_not_hidden_by_success_percentiles(self):
        result = load.summary([{'ok': True, 'elapsed_ms': 10, 'http_status':200},
                               {'ok': False, 'elapsed_ms': 2000, 'http_status':500}], 1500)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['reference_comparison'], 'MISSED_OR_ERRORS')
        self.assertEqual(result['successful_ms']['p99'], 10)
        self.assertGreater(result['all_attempts_ms']['p99'], 1500)
    def test_no_invented_budgets(self):
        self.assertEqual(load.REFERENCE_BUDGET_MS, {'sales_analytics':1500})
        self.assertEqual(load.OWNER_MONITORING_REFERENCE_MS, 2500)
        self.assertEqual(load.summary([])['reference_comparison'], 'NOT_APPLICABLE_NO_DEFINED_BUDGET')
        self.assertEqual(load.summary([],1500)['reference_comparison'], 'NOT_MEASURED')
    def test_required_endpoint_set(self):
        self.assertEqual({(method,path) for _,method,path in load.ENDPOINTS}, {
            ('GET','/products'), ('POST','/orders'), ('GET','/orders'),
            ('GET','/orders/{order_id}/delivery'), ('GET','/analytics/sales'),
            ('GET','/exports/orders.csv'), ('GET','/transactions'), ('GET','/invoices')})
    def test_200_parallel_requests_have_100_shared_idempotency_keys(self):
        payloads = load.burst_payloads('PRD-FIXTURE', 'unit-run')
        barrier = threading.Barrier(200)
        def job(body):
            barrier.wait(timeout=20)
            return body['client_order_id']
        with ThreadPoolExecutor(max_workers=200) as pool:
            results = list(pool.map(job, payloads))
        self.assertEqual(len(results),200)
        self.assertEqual(len(set(results)),100)
        self.assertTrue(all(results.count(key)==2 for key in set(results)))
    def response(self, body, status=200, headers=None):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = status
        response.headers = headers or {'Content-Type':'application/json'}
        response.iter_content.return_value = [json.dumps(body).encode()]
        session = MagicMock()
        session.__enter__.return_value = session
        session.request.return_value = response
        return session
    def test_transport_redacts_and_refuses_redirects(self):
        session = self.response({'ok':True,'data':{'private':'NEVER-SAVE-THIS'}})
        cfg = {'base_url':'https://stage.example.invalid/api/v1','api_token':'PRIVATE-TOKEN'}
        with patch('requests.Session',return_value=session):
            result=load.http_request(cfg,'GET','/products')
        self.assertTrue(result['ok'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('NEVER', json.dumps(result))
        self.assertFalse(session.request.call_args.kwargs['allow_redirects'])
        self.assertTrue(session.request.call_args.kwargs['verify'])
        self.assertFalse(session.trust_env)
    def test_untrusted_error_text_not_in_report(self):
        session=self.response({'ok':False,'error':{'code':'SECRET-KEY','message':'PASSWORD'}},500)
        with patch('requests.Session', return_value=session):
            result=load.http_request({'base_url':'https://example.invalid','api_token':'secret'}, 'GET','/products')
        self.assertFalse(result['ok'])
        self.assertNotIn('PASSWORD',json.dumps(result))
        self.assertNotIn('SECRET-KEY',json.dumps(result))
    def test_order_idempotency_response_contract(self):
        payload={'client_order_id':'load.unit.bench.0'}
        for client, oid, ok in [(payload['client_order_id'],'ORD-00000001',True),('other','ORD-00000001',False),(payload['client_order_id'],'SECRET',False)]:
            with patch('requests.Session',return_value=self.response({'ok':True,'data':{'client_order_id':client,'order_id':oid}},201)):
                result=load.http_request({'base_url':'https://example.invalid','api_token':'secret'},'POST','/orders',payload)
            self.assertEqual(result['ok'],ok)
            if not ok:self.assertIsNone(result['order_id'])
    def test_transport_exception_and_wrong_canary_fail_safely(self):
        cfg={'base_url':'https://example.invalid','api_token':'test-private-token'}
        with patch('requests.Session',side_effect=TimeoutError('DO-NOT-PRINT-THIS')):
            result=load.http_request(cfg,'GET','/products')
        self.assertFalse(result['ok'])
        self.assertNotIn('DO-NOT-PRINT',json.dumps(result))
        with patch('requests.Session',return_value=self.response({'ok':True,'data':{'product_id':'PRD-WRONG'}})):
            result=load.http_request(cfg,'GET','/my-products/PRD-EXPECTED',expected_product='PRD-EXPECTED')
        self.assertFalse(result['ok'])

    def test_delivery_success_requires_items(self):
        for items,ok in [([],False),(['SYNTHETIC'],True)]:
            with patch('requests.Session',return_value=self.response({'ok':True,'data':{'delivery':{'delivery_status':'delivered','items':items}}})):
                result=load.http_request({'base_url':'https://example.invalid','api_token':'secret'},'GET','/orders/ORD-00000001/delivery')
            self.assertEqual(result['ok'],ok)
    def test_expected_stock_rejection_separate_from_success(self):
        with patch('requests.Session',return_value=self.response({'ok':False,'error':{'code':'insufficient_stock'}},422)):
            result=load.http_request({'base_url':'https://example.invalid','api_token':'secret'},'POST','/orders',{'client_order_id':'x'})
        self.assertFalse(result['ok'])
        self.assertTrue(result['expected_stock_rejection'])

class EvidenceTests(unittest.TestCase):
    def fixtures(self):
        before={'wallet':1000,'ledger_count':0,'ledger_cents':0,'stock':{'total':1,'available':1,'sold':0},'orders':[],'claims':[],'events':[],'debits':[]}
        after={'wallet':900,'ledger_count':1,'ledger_cents':-100,'stock':{'total':1,'available':0,'sold':1},
               'orders':[{'id':1,'reseller_id':9,'client_order_id':'load.unit.burst.0','request_id':'req','quantity':1,'unit_price_cents':100}],
               'claims':[{'order_id':1,'n':1}],
               'debits':[{'reference':'rapi.9.req','amount_cents':-100}],
               'events':[{'aggregate_id':'ORD-00000001','event_type':event,'status':'delivered','http_delivered':True} for event in ('order.created','delivery.completed')]}
        return before,after,[{'order_id':'ORD-00000001'}]
    def test_snapshot_is_readonly_and_receiver_specific(self):
        conn=MagicMock()
        values=[None, {'balance_cents':1000}, {'n':0,'cents':0},
                {'total':1,'available':1,'sold':0}, [], [], [], []]
        def execute(sql, params=()):
            result=MagicMock()
            value=values.pop(0)
            result.fetchone.return_value=value
            result.fetchall.return_value=value
            return result
        conn.execute.side_effect=execute
        result=verify.snapshot(conn,9,2,'load.unit.burst.',webhook_id=42)
        self.assertEqual(result['stock']['available'],1)
        queries=[call.args[0] for call in conn.execute.call_args_list]
        self.assertEqual(queries[0],'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        self.assertTrue(all(q.startswith(('SELECT','SET TRANSACTION')) for q in queries))
        event_call=next(call for call in conn.execute.call_args_list if 'EXISTS(SELECT 1 FROM webhook_delivery_log' in call.args[0])
        self.assertIn('l.webhook_id=COALESCE(%s,l.webhook_id)',event_call.args[0])
        self.assertEqual(event_call.args[1][0],42)

    def test_coherent_financial_and_worker_evidence(self):
        self.assertTrue(verify.assess(*self.fixtures())['verified'])
    def test_marked_delivered_without_http_log_is_not_proof(self):
        before,after,responses=self.fixtures()
        after['events'][0]['http_delivered']=False
        self.assertFalse(verify.assess(before,after,responses)['verified'])
    def test_no_orders_is_not_proof(self):
        before,after,responses=self.fixtures()
        after['orders']=[]
        after['events']=[]
        self.assertFalse(verify.assess(before,after,[])['verified'])
    def test_duplicate_events_stock_or_debit_anomalies_fail(self):
        for mutation in ('wallet','debit','stock','event','http','client'):
            before,after,responses=self.fixtures()
            if mutation=='wallet':after['wallet']=800
            if mutation=='debit':after['debits'][0]['reference']='unrelated'
            if mutation=='stock':after['claims'][0]['n']=2
            if mutation=='event':after['events'].append(copy.deepcopy(after['events'][0]))
            if mutation=='http':responses=[]
            if mutation=='client':after['orders'].append(copy.deepcopy(after['orders'][0]))
            with self.subTest(mutation=mutation):
                self.assertFalse(verify.assess(before,after,responses)['verified'])

class TinyRealSeedTests(unittest.TestCase):
    """Real SQLite application services; safety guard mocked, NEVER load evidence."""
    def test_tiny_seed_rerun_preserves_ledger_and_stock(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ.setdefault('DATA_ENCRYPTION_KEY', 'test-only-load-fixture')
            import config
            old_config=config.DB_PATH
            config.DB_PATH=str(Path(directory)/'seed.sqlite')
            import database.db as db
            db.close_thread_connection()
            old_db=db.DB_PATH
            db.DB_PATH=config.DB_PATH
            db._tls=threading.local()
            self.addCleanup(setattr,config,'DB_PATH',old_config)
            self.addCleanup(setattr,db,'DB_PATH',old_db)
            self.addCleanup(db.close_thread_connection)
            guard=MagicMock()
            guard.__enter__.return_value=guard
            guard.execute.return_value.fetchone.return_value={'ok':True}
            report=Path(directory)/'evidence.json'
            cfg={'confirmed_database_name':'mock-only'}
            argv=['seed_staging_load.py','--config','not-read','--namespace','tiny-unit',
                  '--orders','4','--stock','8','--resellers','2','--limited-stock','2','--apply','--report',str(report)]
            plan={'orders':4,'stock':8,'resellers':2,'orders_per_reseller_max':2}
            with patch.dict(os.environ,{'DATA_ENCRYPTION_KEY':'test-only-load-fixture','BOT_TOKEN':'','ADMIN_ID':'0'}), \
                 patch.object(sys,'argv',argv),patch.object(seed,'private_config',return_value=cfg), \
                 patch.object(seed,'guarded_database',return_value=guard),patch.object(seed,'application_environment'), \
                 patch.object(seed,'seed_plan',return_value=plan):
                self.assertIsNone(seed.main())
                db.get_cursor().execute('SELECT COUNT(*),SUM(amount_cents) FROM reseller_wallet_transactions')
                ledger_before=db.get_cursor().fetchone()
                self.assertIsNone(seed.main())
                cur=db.get_cursor()
                cur.execute('SELECT COUNT(*),SUM(amount_cents) FROM reseller_wallet_transactions')
                self.assertEqual(cur.fetchone(),ledger_before)
                result=json.loads(report.read_text())
                self.assertEqual(result['actual_orders'],4)
                self.assertEqual(result['reconciliation_discrepancies'],0)
                cur.execute('SELECT delivery FROM product_stock LIMIT 1')
                self.assertTrue(cur.fetchone()[0].startswith('enc:'))
                cur.execute('SELECT COUNT(*) FROM reseller_api_keys')
                self.assertEqual(cur.fetchone()[0],3)
                cur.execute("UPDATE products SET fulfillment_type='external_api' WHERE name=?", ('LOADTEST:tiny-unit:history',))
                db.get_conn().commit()
                with self.assertRaisesRegex(common.SafetyError,'seed_product_modified'):
                    seed.main()
            db.close_thread_connection()

if __name__=='__main__':unittest.main()
