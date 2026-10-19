"""Native invoice settlement gate, enabled only by test-owned private PG harness."""
import os
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
import threading,unittest
from unittest.mock import patch

@unittest.skipUnless(os.getenv('OGGY_NATIVE_PG_NONCE'),'Run tools/run_deposit_postgres.py for private native PostgreSQL.')
class NativeInvoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root=Path(os.environ['OGGY_NATIVE_PG_ROOT']).resolve()
        if not root.name.startswith('oggy-native-pg-') or (root/'owner-marker').read_text()!=os.environ['OGGY_NATIVE_PG_NONCE']:
            raise RuntimeError('Test server ownership missing.')
        u=urlsplit(os.environ['DATABASE_URL']);q=parse_qs(u.query)
        if u.hostname or u.path!='/oggystore_invoice_test' or q.get('host')!=[str(root/'socket')]:raise RuntimeError('Private test server only.')
        import psycopg
        with psycopg.connect(os.environ['DATABASE_URL'],autocommit=True) as c:
            if Path(c.execute('SHOW data_directory').fetchone()[0]).resolve()!=root/'data':raise RuntimeError('Ownership mismatch.')
        import config
        config.DB_PATH=str(root/'unused.sqlite')
        from database import db,pgcompat,reseller_db as rdb
        if not pgcompat.use_postgres():raise RuntimeError('Native backend required.')
        db.DB_PATH=config.DB_PATH;db.init_db()
        from payments import deposit_settlement as shared,reseller_deposits as service
        cls.db,cls.pg,cls.rdb,cls.shared,cls.service=db,pgcompat,rdb,shared,service;cls.counter=0
    @classmethod
    def tearDownClass(cls):cls.db.close_thread_connection()
    def setUp(self):
        type(self).counter+=1;self.uid=88210000+self.counter
        self.db.ensure_user_id(self.uid);self.rs=self.rdb.create_reseller('Native invoices',self.uid,'invoice-test');self.rid=self.rs['id'];self.rdb.set_reseller_status(self.rid,'active')
        self.addCleanup(self.db.close_thread_connection)
        for target,value in [('config.USDT_BEP20_ADDRESS','0x'+'1'*40),('config.BINANCE_PAY_ID','123456789')]:
            p=patch(target,value);p.start();self.addCleanup(p.stop)
        p=patch.object(self.service,'rails',return_value=[('bep20','BEP20'),('binance','Binance')]);p.start();self.addCleanup(p.stop)
    def scalar(self,sql,*args):
        c=self.db.get_cursor();c.execute(sql,args);return c.fetchone()[0]
    def deposit(self,method='bep20'):return self.service.create_session(self.uid,method,'10')
    def tx(self,method):return '0x'+f'{self.uid:064x}' if method=='bep20' else f'BINANCE-{self.uid}'
    def credit(self,d,tx=None):return self.service.credit_verified(d['id'],d['method'],tx or self.tx(d['method']),d['expected_amount'],address=d['address'],notes=d['memo'] or '')
    def race(self,functions):
        barrier=threading.Barrier(len(functions));results=[];errors=[]
        def run(f):
            try:barrier.wait(timeout=15);results.append(f())
            except Exception as e:errors.append(type(e).__name__)
            finally:self.db.close_thread_connection()
        ts=[threading.Thread(target=run,args=(f,)) for f in functions]
        for t in ts:t.start()
        for t in ts:t.join(timeout=30)
        self.assertFalse(any(t.is_alive() for t in ts));self.assertEqual(errors,[]);return results
    def clean(self):
        from reseller_api.services.reconciliation import reconcile_reseller
        self.assertEqual(reconcile_reseller(self.rid),[])
    def test_native_bootstrap_no_retired_schema(self):
        self.rdb.ensure_reseller_tables();self.rdb.ensure_reseller_tables()
        self.assertIn('PostgreSQL',self.scalar('SELECT version()'))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'reseller_bep20_%'"),0)
    def test_concurrent_reseller_bep_credit_once(self):
        d=self.deposit();r=self.race([lambda:self.credit(d)]*6)
        self.assertEqual(sum(x['result']=='posted' for x in r),1);self.assertEqual(self.rdb.get_wallet_balance_cents(self.rid),1000);self.clean()
    def test_concurrent_reseller_binance_credit_once(self):
        d=self.deposit('binance');r=self.race([lambda:self.credit(d)]*6)
        self.assertEqual(sum(x['result']=='posted' for x in r),1);self.assertEqual(self.rdb.get_wallet_balance_cents(self.rid),1000);self.clean()
    def test_customer_reseller_cross_claim_race(self):
        d=self.deposit();uid=self.uid+10000000;self.db.ensure_user_id(uid);cid,amt,_=self.shared.customer_invoice(uid,10,'bep20');tx=self.tx('bep20')
        r=self.race([lambda:self.credit(d),lambda:self.shared.settle_customer(cid,'bep20',tx,amt)])
        self.assertEqual(sum(x.get('posted',x.get('result')=='posted') for x in r),1)
        self.assertEqual(round(self.db.get_balance(uid)*100)+self.rdb.get_wallet_balance_cents(self.rid),1000);self.clean()
    def test_customer_binance_concurrent_credit_once(self):
        cid,amt,memo=self.shared.customer_invoice(self.uid,10,'binance');tx=self.tx('binance')
        r=self.race([lambda:self.shared.settle_customer(cid,'binance',tx,10,notes=memo)]*6)
        self.assertEqual(sum(x['posted'] for x in r),1);self.assertEqual(self.db.get_balance(self.uid),10)
    def test_ledger_failure_rolls_back_then_retry(self):
        d=self.deposit()
        with patch.object(self.rdb,'post_wallet_transaction',side_effect=RuntimeError('test-only ledger failure')):
            with self.assertRaises(RuntimeError):self.credit(d)
        self.assertEqual(self.service.row(d['id'])['status'],'pending');self.assertEqual(self.rdb.get_wallet_balance_cents(self.rid),0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM processed_txs WHERE tx_hash=?',self.tx('bep20')),0)
        self.assertEqual(self.credit(d)['result'],'posted');self.clean()
    def test_audit_failure_rolls_back_then_retry(self):
        d=self.deposit()
        with patch.object(self.rdb,'log_security_event',side_effect=RuntimeError('test-only audit failure')):
            with self.assertRaises(RuntimeError):self.credit(d)
        self.assertEqual(self.service.row(d['id'])['status'],'pending');self.assertEqual(self.rdb.get_wallet_balance_cents(self.rid),0)
        self.assertEqual(self.credit(d)['result'],'posted');self.clean()
    def test_retired_reservation_is_not_replayed(self):
        d=self.deposit();c=self.db.get_cursor();c.execute('CREATE TABLE reseller_bep20_claims(tx_hash TEXT PRIMARY KEY,chain_id INTEGER)');self.db.get_conn().commit()
        try:
            c.execute('INSERT INTO reseller_bep20_claims VALUES(?,56)',(self.tx('bep20'),));self.db.get_conn().commit()
            self.assertEqual(self.credit(d)['result'],'payment_used');self.assertEqual(self.rdb.get_wallet_balance_cents(self.rid),0)
        finally:c.execute('DROP TABLE reseller_bep20_claims');self.db.get_conn().commit()

if __name__=='__main__':unittest.main()
