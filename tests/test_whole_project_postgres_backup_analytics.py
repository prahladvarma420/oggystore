"""Real pgcompat/backup/analytics code; only the external PostgreSQL driver is a
strict deterministic stand-in. This is NOT a live PostgreSQL integration test.
Browser/Flask probes use isolated SQLite and no external services.
"""
import contextlib, gzip, json, logging, tempfile, threading, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import tests.test_phase16_admin_fail_order as fixture
import tests.test_whole_project_browser as browser_fixture
from database import pgcompat, reseller_db as rdb
from utils import backup

class DriverFailure(RuntimeError):
    sqlstate='42883'

class RawDriver:
    """DB-API boundary double: preserves real _PgConn/_PgCursor buffering."""
    closed=False;broken=False
    def __init__(self,fail_fetch=False):self.statements=[];self.fail_fetch=fail_fetch;self.depth=0;self.ends=[]
    @contextlib.contextmanager
    def transaction(self):
        self.depth+=1
        try:yield
        except Exception:self.ends.append('rollback');raise
        else:self.ends.append('commit')
        finally:self.depth-=1
    def cursor(self):return RawCursor(self)
    def close(self):self.closed=True
    def rollback(self):self.ends.append('rollback')

class RawCursor:
    def __init__(self,raw):self.raw=raw;self.description=None;self.rows=[];self.rowcount=0
    def execute(self,sql,params=None):
        self.raw.statements.append((sql,params));text=sql.lower()
        if 'strftime(' in text:raise DriverFailure('test-only-private-driver-details')
        if text.startswith('set transaction'):self.description=None;self.rows=[];return
        if 'information_schema.tables' in text:cols=['table_name'];rows=[('resellers',),('reseller_deposits',),('future_table',)]
        elif text.startswith('select *'):cols=['id','value'];rows=[(1,'test-fixture-row')]
        elif 'avg(case when status=' in text:
            cols=['total','delivered','pending','dead','retried','avg_latency'];rows=[(2,1,1,0,0,12.0)]
        elif 'group by event_type' in text:cols=['event_type','count'];rows=[('delivery.completed',1),('delivery.pending',1)]
        elif 'group by code' in text:cols=['code','count'];rows=[(200,1)]
        elif 'from webhook_delivery_log' in text:cols=['total','ok','failed','retried'];rows=[(1,1,0,0)]
        else:raise AssertionError('Unexpected test-driver SQL shape')
        self.description=[(c,None,None,None,None,None,None) for c in cols];self.rows=rows;self.rowcount=len(rows)
    def fetchall(self):
        if self.raw.fail_fetch and self.description and self.description[0][0]=='id':raise DriverFailure('test-only-private-fetch-details')
        return self.rows
    def close(self):pass

def connection(fail_fetch=False):
    raw=RawDriver(fail_fetch);conn=pgcompat._PgConn.__new__(pgcompat._PgConn)
    conn._pg=raw;conn._lock=threading.RLock();conn._transaction_depth=0
    conn._psycopg=SimpleNamespace(OperationalError=ConnectionError,InterfaceError=ConnectionError)
    return conn,raw

@contextlib.contextmanager
def postgres_analytics_boundary():
    actual=rdb._webhook_analytics
    def through_driver(*args,**kwargs):
        conn,raw=connection()
        try:
            with patch.object(rdb,'get_cursor',return_value=conn.cursor()),patch.object(rdb,'_pgcompat',SimpleNamespace(use_postgres=lambda:True)):
                return actual(*args,**kwargs)
        finally:conn.close()
    with patch.object(rdb,'_webhook_analytics',side_effect=through_driver):yield

class PostgresAdapterBackupTests(unittest.TestCase):
    def test_cursor_exposes_dbapi_description_after_buffering(self):
        conn,raw=connection();cur=conn.cursor();cur.execute('SELECT * FROM "resellers"')
        self.assertEqual([d[0] for d in cur.description],['id','value']);self.assertEqual(list(cur.fetchone()),[1,'test-fixture-row'])
    def test_backup_uses_real_pg_adapter_columns_rows_and_one_snapshot(self):
        conn,raw=connection()
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'backup.json.gz'
            with patch.object(backup,'use_postgres',return_value=True),patch.object(pgcompat,'use_postgres',return_value=True),patch.object(pgcompat,'connect',return_value=conn):backup._dump_postgres(target)
            value=json.loads(gzip.decompress(target.read_bytes()))
            self.assertEqual(set(value['tables']),{'resellers','reseller_deposits','future_table'})
            self.assertTrue(all(t=={'columns':['id','value'],'rows':[[1,'test-fixture-row']]} for t in value['tables'].values()))
            self.assertEqual(raw.ends,['commit']);self.assertTrue(raw.closed)
            self.assertIn('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY',raw.statements[0][0])
    def test_failed_fetch_is_not_silently_an_empty_success(self):
        conn,raw=connection(fail_fetch=True)
        with self.assertRaises(DriverFailure):conn.cursor().execute('SELECT * FROM "resellers"')
    def test_failed_fetch_rolls_back_snapshot_and_leaves_no_file_or_raw_secret(self):
        conn,raw=connection(fail_fetch=True)
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'failed.json.gz'
            with patch.object(backup,'use_postgres',return_value=True),patch.object(pgcompat,'use_postgres',return_value=True),patch.object(pgcompat,'connect',return_value=conn):
                with self.assertRaises(RuntimeError) as caught:backup._dump_postgres(target)
            self.assertFalse(target.exists());self.assertEqual(raw.ends,['rollback']);self.assertNotIn('private-fetch',str(caught.exception));self.assertTrue(raw.closed)
    def test_pg_webhook_latency_query_no_sqlite_function_and_real_aggregation(self):
        conn,raw=connection()
        with patch.object(rdb,'get_cursor',return_value=conn.cursor()),patch.object(rdb,'_pgcompat',SimpleNamespace(use_postgres=lambda:True)):
            data=rdb._webhook_analytics('reseller_id=?',(123,),30,since='2026-01-01 00:00:00')
        self.assertEqual((data['events_total'],data['delivered'],data['pending'],data['avg_delivery_seconds']),(2,1,1,12.0))
        sql=raw.statements[0][0];self.assertIn('EXTRACT(EPOCH',sql);self.assertNotIn('strftime',sql);self.assertIn('%s',sql)
        self.assertEqual(raw.statements[0][1],(123,'2026-01-01 00:00:00'))

class WebAdminAnalyticsBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import web_admin.app as wa
        cls.wa=wa;wa.DB_PATH=fixture._db_path
    def setUp(self):self.client=fixture.Phase16AdminFailOrderTests()._owner()
    def test_owner_webhook_analytics_route_through_real_pg_adapter(self):
        with postgres_analytics_boundary():response=self.client.get('/api/monitoring/webhook-analytics')
        self.assertEqual(response.status_code,200);self.assertEqual(response.get_json()['webhooks']['avg_delivery_seconds'],12.0)
    def test_reseller_webhooks_route_through_real_pg_adapter(self):
        res=rdb.create_reseller('PG boundary reseller '+str(fixture._n()))
        with postgres_analytics_boundary():response=self.client.get('/api/resellers/'+res['public_id']+'/webhooks')
        self.assertEqual(response.status_code,200);self.assertEqual(response.get_json()['health']['delivered'],1)
    def test_failed_monitoring_read_returns_safe_json_and_trace_reference(self):
        with patch.object(rdb,'get_platform_webhook_analytics',side_effect=DriverFailure('test-only-private-driver-details')):
            with self.assertLogs(self.wa.app.logger.name,level='ERROR') as logs:response=self.client.get('/api/monitoring/webhook-analytics')
        self.assertEqual(response.status_code,500);data=response.get_json();self.assertFalse(data['ok']);self.assertTrue(data['error_id'])
        self.assertEqual(response.headers['X-Error-ID'],data['error_id'])
        self.assertNotIn('test-only-private-driver-details',response.get_data(as_text=True)+str(logs.output));self.assertIn('42883',str(logs.output))

class AnalyticsErrorBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):browser_fixture.WholeProjectBrowserTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):browser_fixture.WholeProjectBrowserTests.tearDownClass.__func__(cls)
    def setUp(self):browser_fixture.WholeProjectBrowserTests.setUp(self)
    def test_non_json_500_explains_http_status_without_html_or_secret(self):
        self.page.route('**/api/test-only-non-json',lambda r:r.fulfill(status=500,content_type='text/html',body='<h1>test-only-private-server-error</h1>'))
        result=self.page.evaluate("async()=>{try{await api('/api/test-only-non-json');return 'resolved'}catch(e){return e.message}}")
        self.assertIn('HTTP 500',result);self.assertNotIn('test-only-private',result);self.assertNotEqual(result,'Bad response')
    def test_json_500_preserves_safe_error_reference(self):
        self.page.route('**/api/test-only-json',lambda r:r.fulfill(status=500,content_type='application/json',body=json.dumps({'ok':False,'error':'Internal server error','error_id':'test-reference-123'})))
        result=self.page.evaluate("async()=>{try{await api('/api/test-only-json');return 'resolved'}catch(e){return e.message}}")
        self.assertIn('test-reference-123',result)

if __name__=='__main__':unittest.main()
