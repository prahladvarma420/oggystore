"""Phase16–17 security delta: real existing seams, isolated inherited assertions.
No production credentials, providers, Telegram or deployment writes. Child cohorts
retain prior assertions verbatim and avoid import-time DB-path contamination.
Nested child counts are reported separately, never folded into top-level totals.
"""
from __future__ import annotations
import contextlib
import gzip
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

ROOT=Path(__file__).resolve().parents[1]
os.environ['DATABASE_URL']=''
os.environ['BOT_TOKEN']=''
os.environ['PYTHON_DOTENV_DISABLED']='1'
os.environ.setdefault('DATA_ENCRYPTION_KEY','test-only-delta-encryption')

COHORTS={
 'admin': ('tests.test_phase16_security_audit.Phase16SecurityAuditTests.', (
  'test_authorization_matrix_lifecycle_and_secret_config',
  'test_real_api_idor_foreign_order_indistinguishable_from_missing',
  'test_owner_does_not_apply_reseller_actions_to_customer_orders',
  'test_double_click_and_two_browser_terminal_races',
  'test_partial_refund_cap_terminal_actions_and_statements')),
 'fulfillment': ('tests.test_phase16_security_audit.Phase16SecurityAuditTests.', (
  'test_supplier_ssrf_private_mixed_addresses_and_rebinding',
  'test_supplier_https_timeouts_retry_budgets_and_pinning',
  'test_supplier_redirect_never_followed',
  'test_supplier_transient_and_permanent_retry_budget',
  'test_supplier_network_outside_transaction_and_committed_order',
  'test_supplier_adversarial_log_and_raw_exception_privacy')),
 'deposits': ('tests.test_phase17_reseller_deposits.ResellerDepositTests.', (
  'test_repeat_is_exactly_once_and_one_event','test_transaction_cannot_credit_two_resellers',
  'test_customer_then_reseller_collision','test_reseller_then_customer_collision',
  'test_cross_table_concurrent_claim_one_winner','test_atomic_security_failure_and_resume',
  'test_wrong_network_rpc_is_rejected','test_trc_provider_validates_destination_asset_and_precision',
  'test_bep_provider_filter_contract_confirmation_destination',
  'test_binance_actual_checker_asset_allowlist')),
 'email': ('tests.test_phase16_email_products.Phase16EmailProductsTests.', (
  'test_email_validation_matrix_and_no_email_in_errors',
  'test_redaction_scan_real_responses_events_exports_analytics_logs',
  'test_replay_different_email_keeps_original_ciphertext_and_one_queue_row',
  'test_no_encryption_key_rolls_back_order_and_wallet')),
 'catalog': ('tests.test_phase17_telegram_catalog.TelegramCatalogTests.', (
  'test_inactive_status_block_and_no_reseller_denied',
  'test_authorization_rechecked_for_pagination_and_detail',
  'test_cross_reseller_idor_and_invalid_detail_identifiers',
  'test_html_escape_and_no_price_provenance_or_sensitive_fields')),
 'statistics_docs': ('tests.test_phase17_telegram_stats_docs.TelegramStatsDocsTests.', (
  'test_inactive_blocked_and_unlinked_authorization_every_view',
  'test_authorization_revalidated_between_reads_and_refreshes',
  'test_cross_reseller_usage_wallet_sales_never_combined',
  'test_large_fixtures_messages_bounded_no_sensitive_fields')),
 'notifications': ('tests.test_phase17_notifications.NotificationsTests.', (
  'test_html_length_unicode_pii_and_secret_scan_all_builders',
  'test_transport_timeout_budget_and_sanitized_adversarial_logs',
  'test_mock_bot_receives_html_once_no_real_key_or_delivery_fields',
  'test_failed_repeated_unknown_or_missing_status_no_notification',
  'test_fail_cancel_routes_shared_messages_and_retry_no_duplicates')),
}
PRIOR_AUDITS=('tests.test_phase12_order_security_audit',
 'tests.test_phase13_api_security_audit','tests.test_phase14_analytics_audit',
 'tests.test_phase15_financial_security_audit')

CHILD='''import json,sys,unittest
from pathlib import Path
suite=unittest.defaultTestLoader.loadTestsFromNames(json.loads(sys.argv[1]))
r=unittest.TextTestRunner(verbosity=1).run(suite)
Path(sys.argv[2]).write_text(json.dumps(dict(tests=r.testsRun,failures=len(r.failures),errors=len(r.errors),skipped=len(r.skipped))))
sys.exit(0 if r.wasSuccessful() and r.testsRun and not r.skipped else 1)
'''
class IsolatedSecurityDeltaTests(unittest.TestCase):
    def run_names(self,names):
        from tools.run_tests import test_environment
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'counts.json'
            result=subprocess.run([sys.executable,'-c',CHILD,json.dumps(names),str(output)],
                cwd=ROOT,env=test_environment(),capture_output=True,text=True,timeout=180)
            self.assertTrue(output.exists(), 'Child did not execute to completion: '+result.stderr[-4000:])
            counts=json.loads(output.read_text())
            self.assertEqual(result.returncode,0,result.stdout[-4000:]+result.stderr[-8000:])
            self.assertGreater(counts['tests'],0)
            self.assertEqual((counts['failures'],counts['errors'],counts['skipped']),(0,0,0))
            print('DELTA_EVIDENCE '+json.dumps({'names':names,**counts},sort_keys=True))
    def cohort(self,name):
        prefix,methods=COHORTS[name]
        self.run_names([prefix+method for method in methods])
    def test_admin_lifecycle_authorization_idor_and_exactly_once(self):self.cohort('admin')
    def test_fulfillment_ssrf_secrets_retry_and_transaction_boundaries(self):self.cohort('fulfillment')
    def test_deposit_trusted_source_replay_and_cross_table_collision(self):self.cohort('deposits')
    def test_buyer_email_validation_encryption_logs_and_replay(self):self.cohort('email')
    def test_telegram_catalog_gates_and_foreign_resources(self):self.cohort('catalog')
    def test_telegram_statistics_docs_gates_and_data_isolation(self):self.cohort('statistics_docs')
    def test_notification_content_secret_scan_and_retry_deduplication(self):self.cohort('notifications')
    def test_buyer_email_foreign_and_anonymous_reads(self):
        self.run_names(['tests.test_phase17_security_delta.email_privacy_suite'])
    def test_prior_p12_audit_unchanged(self):self.run_names([PRIOR_AUDITS[0]])
    def test_prior_p13_audit_unchanged(self):self.run_names([PRIOR_AUDITS[1]])
    def test_prior_p14_audit_unchanged(self):self.run_names([PRIOR_AUDITS[2]])
    def test_prior_p15_audit_unchanged(self):self.run_names([PRIOR_AUDITS[3]])

class BackupDeltaRegressionTests(unittest.TestCase):
    """Fail-before/fix-after evidence; SQLite real, PostgreSQL transport mocked."""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.directory=Path(self.tmp.name)
        from utils import backup
        self.b=backup
        for name,value in [('DB_PATH',str(self.directory/'live.db')),('BACKUP_DIR',str(self.directory/'backups'))]:
            p=patch.object(backup,name,value);p.start();self.addCleanup(p.stop)
    def test_committed_wal_is_in_consistent_sqlite_backup(self):
        writer=sqlite3.connect(self.b.DB_PATH)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL');writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('CREATE TABLE reseller_delta(value TEXT)');writer.commit()
        writer.execute("INSERT INTO reseller_delta VALUES('committed-in-wal')");writer.commit()
        self.assertGreater(Path(self.b.DB_PATH+'-wal').stat().st_size,0)
        with patch.object(self.b,'use_postgres',return_value=False):path=self.b.create_backup()
        with contextlib.closing(sqlite3.connect(path)) as restored:
            self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(restored.execute('SELECT value FROM reseller_delta').fetchone()[0],'committed-in-wal')
        if os.name!='nt':self.assertEqual(Path(path).stat().st_mode & 0o077,0)
    def pg_connection(self,fail=False):
        from database.pgcompat import _PgConn
        cur=MagicMock();conn=_PgConn.__new__(_PgConn)
        conn._transaction_depth=0;conn._lock=threading.RLock();conn._pg=MagicMock()
        conn.cursor=MagicMock(return_value=cur);conn.close=MagicMock()
        tables=['resellers','reseller_wallet_transactions','reseller_deposits','processed_txs','new_reseller_table']
        def execute(sql,*args):
            self.assertEqual(conn._transaction_depth,1,'Entire snapshot must disable reconnect outside atomic')
            if sql.startswith('SELECT *'):
                if fail:raise RuntimeError('test-secret-driver-credential')
                cur.description=[('id',),('value',)]
                cur.fetchall.return_value=[(1,'synthetic')]
            else:cur.fetchall.return_value=[(name,) for name in tables]
        cur.execute.side_effect=execute
        return conn,cur,tables
    def test_postgres_backup_enumerates_new_tables_and_uses_one_readonly_snapshot(self):
        conn,cur,tables=self.pg_connection();path=self.directory/'snapshot.json.gz'
        with patch.object(self.b,'use_postgres',return_value=True),patch('database.pgcompat.connect',return_value=conn):
            self.b._dump_postgres(path)
        data=json.loads(gzip.decompress(path.read_bytes()))
        self.assertEqual(set(data['tables']),set(tables))
        self.assertTrue(all(len(t['rows'])==1 for t in data['tables'].values()))
        queries=[c.args[0] for c in cur.execute.call_args_list]
        self.assertIn('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY',queries)
        conn._pg.transaction.assert_called_once()
        self.assertIsNone(conn._pg.transaction.return_value.__exit__.call_args.args[0])
        self.assertEqual(conn._transaction_depth,0)
        conn.close.assert_called_once()
    def test_failed_postgres_table_aborts_backup_no_partial_success_or_secret(self):
        conn,cur,_=self.pg_connection(fail=True);path=self.directory/'failed.json.gz'
        with patch.object(self.b,'use_postgres',return_value=True),patch('database.pgcompat.connect',return_value=conn):
            with self.assertRaises(RuntimeError) as caught:self.b._dump_postgres(path)
        self.assertNotIn('test-secret-driver',str(caught.exception))
        self.assertFalse(path.exists())
        self.assertIs(conn._pg.transaction.return_value.__exit__.call_args.args[0],RuntimeError)
        self.assertEqual(conn._transaction_depth,0)
        conn.close.assert_called_once()

class RealBoundaryDeltaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tests.test_phase16_admin_fail_order as fixture
        cls.f=fixture;fixture.db.init_db();fixture.Phase16AdminFailOrderTests.setUpClass()
    @classmethod
    def tearDownClass(cls):cls.f.Phase16AdminFailOrderTests.tearDownClass()
    def setUp(self):
        self.fx=self.f.Phase16AdminFailOrderTests('test_successful_fail_full_effects')
        self.owner=self.fx._owner();self.db=self.f.db;self.rdb=self.f.rdb
    def test_runbook_routes_and_all_twelve_topics_match_registered_code(self):
        import re
        import web_admin.app as wa
        text=(ROOT/'OPERATIONS.md').read_text()
        topics=re.findall(r'^## (\d+)\.',text,re.M)
        self.assertEqual(topics,[str(n) for n in range(1,13)])
        operations=re.findall(r'<!-- OPS: (GET|POST) ([^ ]+) -->',text)
        self.assertGreaterEqual(len(operations),30)
        for method,pattern in operations:
            path=re.sub(r'\{([^}]+)\}',lambda m:'1' if m[1]=='oid' else 'fixture',pattern)
            app=self.f.Phase16AdminFailOrderTests.app if path.startswith('/api/v1/') else wa.app
            endpoint,_=app.url_map.bind('localhost').match(path,method=method)
            self.assertTrue(endpoint)
        self.assertIn('Never use raw SQL',text)
        self.assertIn('24 hours',text)
        self.assertIn('30 seconds',text)
        self.assertIn('no implemented',text.lower())

    def test_invalid_lifecycle_payloads_cannot_mutate_order_wallet_or_outbox(self):
        rs,oid,_=self.fx._make_reseller_order()
        self.db.cur.execute('UPDATE orders SET delivery=NULL WHERE id=?',(oid,));self.db.conn.commit()
        def state():
            self.db.cur.execute('SELECT master_status,delivery FROM orders WHERE id=?',(oid,));row=self.db.cur.fetchone()
            self.db.cur.execute('SELECT COUNT(*) FROM webhook_outbox WHERE aggregate_id=?',(f'ORD-{oid:08d}',))
            events=self.db.cur.fetchone()[0]
            return row,self.rdb.get_wallet_balance_cents(rs['id']),events
        before=state()
        for action in ('fail','cancel'):
            for body in ({},{'reason':''},{'reason':' '},{'reason':1},{'reason':'x'*501},['not-an-object']):
                result=self.owner.post(f'/api/orders/{oid}/{action}',json=body,headers={'X-CSRF-Token':'test-csrf'})
                self.assertEqual(result.status_code,400);self.assertEqual(state(),before)
        for body in ({},{'delivery':None},{'delivery':42},{'delivery':''},{'delivery':'x'*10001}):
            result=self.owner.post(f'/api/orders/{oid}/deliver',json=body,headers={'X-CSRF-Token':'test-csrf'})
            self.assertIn(result.status_code,(400,413));self.assertEqual(state(),before)
    def test_no_client_credit_surface_or_financial_fields_can_forge_a_deposit(self):
        rs,oid,pid=self.fx._make_reseller_order()
        key,secret=self.rdb.create_api_key(rs['id'],'test');headers=self.f._hdr(key,secret)
        api=self.f.Phase16AdminFailOrderTests.client
        before=self.rdb.get_wallet_balance_cents(rs['id'])
        for path in ('/api/v1/deposits/credit','/api/v1/wallet/credit','/api/v1/reseller-deposits/verify'):
            result=api.post(path,headers=headers,json={'txid':'0x'+'a'*64,'actual_amount':'999999','confirmed':True})
            self.assertIn(result.status_code,(404,405))
        public=self.rdb.ensure_product_public_id(pid)
        result=api.post('/api/v1/orders',headers=headers,json={'product_id':public,'quantity':1,'balance':999999,'txid':'a'*64})
        self.assertEqual(result.status_code,400)
        self.assertEqual(self.rdb.get_wallet_balance_cents(rs['id']),before)
    def test_zip_uses_actual_postgres_backend_not_stale_local_database(self):
        import zipfile,config
        from utils import backup
        with tempfile.TemporaryDirectory() as directory:
            directory=Path(directory);stale=directory/'stale.db';stale.write_bytes(b'not-the-live-database')
            snapshot=directory/'snapshot.json.gz';snapshot.write_bytes(gzip.compress(json.dumps({'tables':{'reseller_deposits':{'columns':[],'rows':[]}}}).encode()))
            previous=os.getcwd()
            try:
                os.chdir(directory)
                with patch.object(config,'DB_PATH',str(stale)),patch.object(backup,'create_backup',return_value=str(snapshot)),patch.object(backup,'use_postgres',return_value=True):
                    path=self.db.create_backup_zip()
                with zipfile.ZipFile(path) as z:
                    self.assertIn(snapshot.name,z.namelist());self.assertNotIn(stale.name,z.namelist())
            finally:os.chdir(previous)
    def test_backup_encryption_failure_is_not_reported_as_success(self):
        import web_admin.app as wa
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'private.zip';path.write_bytes(b'test-only-private-backup')
            with patch.object(wa.botdb,'create_backup_zip',return_value=str(path)),patch.dict(os.environ,{'BACKUP_ENCRYPTION_KEY':'test-invalid-fernet-key'}):
                result=self.owner.post('/api/backup/create',json={},headers={'X-CSRF-Token':'test-csrf'})
            self.assertEqual(result.status_code,500)
            self.assertFalse(result.get_json()['ok']);self.assertFalse(path.exists())
            self.assertNotIn('test-invalid-fernet-key',result.get_data(as_text=True))
    def test_successful_configured_backup_encryption_round_trip_and_private_mode(self):
        import web_admin.app as wa
        from cryptography.fernet import Fernet
        key=Fernet.generate_key()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'private.zip';raw=b'test-only-encrypted-archive';path.write_bytes(raw)
            with patch.object(wa.botdb,'create_backup_zip',return_value=str(path)),patch.dict(os.environ,{'BACKUP_ENCRYPTION_KEY':key.decode()}):
                result=self.owner.post('/api/backup/create',json={},headers={'X-CSRF-Token':'test-csrf'})
            self.assertEqual(result.status_code,200);self.assertTrue(result.get_json()['encrypted'])
            encrypted=Path(str(path)+'.enc')
            self.assertEqual(Fernet(key).decrypt(encrypted.read_bytes()),raw)
            self.assertFalse(path.exists())
            if os.name!='nt':self.assertEqual(encrypted.stat().st_mode & 0o077,0)

    def test_backup_contains_every_live_reseller_table_with_counts_and_integrity(self):
        import config
        from utils import backup
        self.fx._make_reseller_order()
        self.db.cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        tables=[r[0] for r in self.db.cur.fetchall()]
        self.db.conn.commit()
        with tempfile.TemporaryDirectory() as directory,patch.object(backup,'DB_PATH',self.db.DB_PATH),patch.object(backup,'BACKUP_DIR',directory),patch.object(backup,'use_postgres',return_value=False):
            path=backup.create_backup()
            with contextlib.closing(sqlite3.connect(path)) as restored:
                actual={r[0] for r in restored.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                self.assertEqual(actual,set(tables))
                self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertEqual(restored.execute('PRAGMA foreign_key_check').fetchall(),[])
                for table in tables:
                    safe='"'+table.replace('"','""')+'"'
                    self.db.cur.execute('SELECT COUNT(*) FROM '+safe)
                    self.assertEqual(self.db.cur.fetchone()[0],restored.execute('SELECT COUNT(*) FROM '+safe).fetchone()[0])
                for name in ('resellers','reseller_wallet','reseller_wallet_transactions','reseller_deposits','processed_txs','webhook_outbox','worker_jobs'):
                    self.assertIn(name,actual)
    def test_postgres_mode_rejects_sqlite_only_restore_instead_of_false_success(self):
        import zipfile
        from database import pgcompat
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'archive.zip'
            with zipfile.ZipFile(path,'w') as z:z.writestr('ignored.db',b'not-postgres')
            with patch.object(pgcompat,'use_postgres',return_value=True):
                ok,message=self.db.restore_backup_zip(str(path))
            self.assertFalse(ok);self.assertIn('PostgreSQL',message)

def email_privacy_suite():
    """Factory is loaded only in its dedicated child, never during discovery."""
    def probe():
        import tests.test_phase16_email_products as email
        email.Phase16EmailProductsTests.setUpClass()
        case=email.Phase16EmailProductsTests('test_m365_pending_then_real_endpoint_completion')
        try:
            case.setUp()
            fixture,oid,_=case.create()
            foreign=case.fx.product(ftype='manual')
            for suffix in ('','/delivery'):
                path=f'/api/v1/orders/ORD-{oid:08d}'+suffix
                for headers,status in (({},401),(foreign[3],404)):
                    response=case.fx.api.get(path,headers=headers)
                    case.assertEqual(response.status_code,status)
                    case.assertNotIn(email.EMAIL,response.get_data(as_text=True))
            email.f.db.cur.execute('SELECT email FROM m365_invites WHERE order_id=?',(oid,))
            encrypted=email.f.db.cur.fetchone()[0]
            case.assertTrue(encrypted.startswith('enc:'))
            case.assertNotIn(email.EMAIL,encrypted)
        finally:
            case.doCleanups()
            email.Phase16EmailProductsTests.tearDownClass()
    return unittest.FunctionTestCase(probe,description='Actual email-gated foreign/anonymous read and encrypted persistence probe')

if __name__=='__main__':unittest.main()
