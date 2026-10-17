"""Opt-in NATIVE PostgreSQL gate; only tools/run_unique_bep20_postgres.py may enable.

The unified runner intentionally isolates SQLite suites and skips this module.
This suite never imports SQLite fixtures or accepts a production database URL.
"""
import os
from pathlib import Path
import tempfile
import threading
import unittest
from dataclasses import replace
from urllib.parse import urlsplit,parse_qs
from unittest.mock import patch

ENABLED=bool(os.getenv('OGGY_NATIVE_PG_NONCE'))


@unittest.skipUnless(ENABLED,'Run python tools/run_unique_bep20_postgres.py for a private native PostgreSQL cluster.')
class NativePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root=Path(os.environ['OGGY_NATIVE_PG_ROOT']).resolve()
        nonce=os.environ['OGGY_NATIVE_PG_NONCE']
        if not root.name.startswith('oggy-native-pg-') or (root/'owner-marker').read_text()!=nonce:
            raise RuntimeError('Disposable database ownership not established.')
        parsed=urlsplit(os.environ['DATABASE_URL']);query=parse_qs(parsed.query)
        if parsed.hostname or parsed.path!='/oggystore_unique_test' or query.get('host')!=[str(root/'socket')]:
            raise RuntimeError('Only the test-owned Unix socket database is permitted.')
        import psycopg
        with psycopg.connect(os.environ['DATABASE_URL'],autocommit=True) as connection:
            if Path(connection.execute('SHOW data_directory').fetchone()[0]).resolve()!=root/'data':
                raise RuntimeError('Server ownership mismatch.')
        import config
        config.DB_PATH=str(root/'unused.sqlite')
        from database import db,pgcompat,reseller_db as rdb
        db.DB_PATH=config.DB_PATH
        if not pgcompat.use_postgres():raise RuntimeError('Native backend is required.')
        cls.db,cls.pg,cls.rdb=db,pgcompat,rdb
        db.init_db()
        from payments import unique_bep20 as core,deposit_settlement as legacy,reseller_deposits as invoices
        cls.core,cls.legacy,cls.invoices=core,legacy,invoices
        cls.counter=0
    @classmethod
    def tearDownClass(cls):cls.db.close_thread_connection()
    def setUp(self):
        type(self).counter+=1
        self.uid=77100000+self.counter
        self.db.ensure_user_id(self.uid)
        self.rs=self.rdb.create_reseller('Native PG reseller',self.uid,'native-test');self.rid=self.rs['id']
        self.rdb.set_reseller_status(self.rid,'active')
        self.cfg=self.core.Settings('0x'+'a'*40,'test-only',f'pg-wallet-{self.uid}',90,1,batch_blocks=10)
        self.address='0x'+f'{self.uid:040x}'
        self.second='0x'+f'{self.uid+1000000000:040x}'
        self.tx='0x'+f'{self.uid:064x}'
        test=self
        class Custody:
            def inventory(self,cfg):return [dict(address=a,provider_ref='pg-ref-'+a[2:],chain_id=56,wallet_ref=cfg.wallet_ref) for a in (test.address,test.second)]
            def acceptance(self,cfg,e,ref):return dict(status='accepted',reference='pg-event-'+e.key,chain_id=56,token=e.token,tx_hash=e.tx_hash,log_index=e.log_index,address=e.recipient,raw_units=str(e.raw_units),wallet_ref=cfg.wallet_ref)
        self.adapter=Custody()
        with self.pg.atomic(self.db.get_conn()):self.db.get_cursor().execute('DELETE FROM reseller_bep20_scan')
        self.core.import_inventory(self.cfg,self.adapter)
        self.core.allocate_address(self.uid,self.cfg)
        self.addCleanup(self.db.close_thread_connection)
        for target,value in [('config.USDT_BEP20_ADDRESS','0x'+'1'*40),('config.BINANCE_PAY_ID','12345678')]:
            p=patch(target,value);p.start();self.addCleanup(p.stop)
        p=patch.object(self.invoices,'rails',return_value=[('bep20','BEP20'),('binance','Binance')]);p.start();self.addCleanup(p.stop)
    def scalar(self,sql,*args):
        c=self.db.get_cursor();c.execute(sql,args);return c.fetchone()[0]
    def event(self,raw=10**18,index=0):
        return self.core.Transfer(56,self.cfg.token,self.tx,index,self.address,raw,95,'0x'+f'{95:064x}')
    def credit(self,raw=10**18,index=0):return self.core.settle_transfer(self.cfg,self.event(raw,index),self.adapter)
    def race(self,functions):
        barrier=threading.Barrier(len(functions));results=[];errors=[]
        def run(fn):
            try:barrier.wait(timeout=15);results.append(fn())
            except Exception as exc:errors.append(type(exc).__name__)
            finally:self.db.close_thread_connection()
        threads=[threading.Thread(target=run,args=(fn,)) for fn in functions]
        for t in threads:t.start()
        for t in threads:t.join(timeout=30)
        self.assertFalse(any(t.is_alive() for t in threads));self.assertEqual(errors,[])
        return results
    def clean(self):
        self.assertEqual(self.core.reconcile_exact(self.rid,self.cfg)['problems'],[])
        from reseller_api.services.reconciliation import reconcile_reseller
        self.assertEqual(reconcile_reseller(self.rid),[])
    def customer(self):
        uid=self.uid+20000000;self.db.ensure_user_id(uid)
        dep,amount,memo=self.legacy.customer_invoice(uid,10,'bep20')
        return uid,dep,amount
    def test_actual_backend_and_idempotent_migration(self):
        self.assertIn('PostgreSQL',self.scalar('SELECT version()'))
        self.rdb.ensure_reseller_tables();self.rdb.ensure_reseller_tables()
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'reseller_bep20_%'"),5)
    def test_concurrent_first_address_allocation(self):
        uid=self.uid+10000000;self.db.ensure_user_id(uid)
        rs=self.rdb.create_reseller('PG concurrent',uid,'pg');self.rdb.set_reseller_status(rs['id'],'active')
        results=self.race([lambda:self.core.allocate_address(uid,self.cfg)]*6)
        self.assertEqual(len({r['address'] for r in results}),1)
        self.assertNotEqual(results[0]['address'],self.address)
    def test_database_unique_ownership_constraint(self):
        with self.assertRaises(Exception):
            with self.pg.atomic(self.db.get_conn()):
                self.db.get_cursor().execute('UPDATE reseller_bep20_addresses SET reseller_id=? WHERE address=?',(self.rid,self.second))
        self.assertEqual(self.core.allocate_address(self.uid,self.cfg)['address'],self.address)
    def test_duplicate_credit_race_and_multiple_logs(self):
        results=self.race([lambda:self.credit()]*6)
        self.assertEqual(results.count('credited'),1);self.assertEqual(results.count('duplicate'),5)
        self.credit(2*10**18,index=1)
        self.assertEqual(self.rdb.get_wallet_balance_cents(self.rid),300);self.clean()
    def test_fractional_concurrent_carry_is_exact(self):
        self.race([lambda i=i:self.credit(3*10**15+1,index=i) for i in range(6)])
        totals=self.core.reconcile_exact(self.rid,self.cfg)
        self.assertEqual(totals['credited_cents'],1)
        self.assertEqual(totals['remainder_units'],str(8*10**15+6));self.clean()
    def test_ledger_failure_rolls_back_then_retry(self):
        with patch.object(self.rdb,'post_wallet_transaction',side_effect=RuntimeError('test ledger failure')):
            with self.assertRaises(RuntimeError):self.credit()
        self.assertEqual(self.rdb.get_wallet_balance_cents(self.rid),0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM reseller_bep20_events WHERE address=?',self.address),0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM reseller_bep20_claims WHERE tx_hash=?',self.tx),0)
        self.assertEqual(self.credit(),'credited');self.clean()
    def test_audit_failure_rolls_back_balance_carry(self):
        with patch.object(self.rdb,'log_security_event',side_effect=RuntimeError('test audit failure')):
            with self.assertRaises(RuntimeError):self.credit(10**18+1)
        self.assertEqual(self.rdb.get_wallet_balance_cents(self.rid),0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM reseller_bep20_carry WHERE reseller_id=?',self.rid),0)
        self.assertEqual(self.credit(10**18+1),'credited');self.clean()
    def test_customer_new_claim_race_one_winner(self):
        uid,dep,amount=self.customer()
        results=self.race([lambda:self.credit(),lambda:self.legacy.settle_customer(dep,'bep20',self.tx,amount)])
        self.assertEqual(sum(x=='credited' if isinstance(x,str) else x['posted'] for x in results),1)
        self.assertFalse(self.rdb.get_wallet_balance_cents(self.rid)>0 and self.db.get_balance(uid)>0);self.clean()
    def test_reseller_invoice_new_claim_race_one_winner(self):
        invoice=self.invoices.create_session(self.uid,'bep20','10.00')
        results=self.race([lambda:self.credit(),lambda:self.invoices.credit_verified(invoice['id'],'bep20',self.tx,invoice['expected_amount'],address=invoice['address'])])
        self.assertEqual(sum(x=='credited' if isinstance(x,str) else x['result']=='posted' for x in results),1);self.clean()
    def test_suspension_incoming_credit_not_confiscated(self):
        self.rdb.set_reseller_status(self.rid,'suspended')
        with self.assertRaises(self.core.DepositUnavailable):self.core.allocate_address(self.uid,self.cfg)
        self.assertEqual(self.credit(),'credited');self.clean()
    def test_committed_incident_cannot_be_bypassed(self):
        self.credit()
        changed=replace(self.event(),block_hash='0x'+'f'*64)
        self.assertEqual(self.core.settle_transfer(self.cfg,changed,self.adapter),'paused_review')
        with self.assertRaises(self.core.DepositUnavailable):
            self.core.settle_transfer(replace(self.cfg,confirmations=20),self.event(index=1),self.adapter)
        self.assertEqual(self.rdb.get_wallet_balance_cents(self.rid),100)


if __name__=='__main__':unittest.main()
