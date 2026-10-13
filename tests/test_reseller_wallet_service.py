import os, sys, tempfile, threading, unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
fd,path=tempfile.mkstemp(prefix='oggy_wallet_',suffix='.db'); os.close(fd)
import config; config.DB_PATH=path
import database.db as db; db.DB_PATH=path; db._tls=threading.local(); db.init_db()
import database.reseller_db as rdb
from reseller_api.context import ApiRequestContext
from reseller_api.services.authentication import authenticate_credentials
from reseller_api.services.wallet import *

class WalletServiceTests(unittest.TestCase):
    n=0
    def setUp(self):
        type(self).n+=1; n=self.n; uid=970000+n
        db.cur.execute('INSERT INTO users(user_id,first_name,username,blocked) VALUES(?,?,?,0)',(uid,'Wallet',f'w{n}')); db.conn.commit()
        self.r=rdb.create_reseller(f'Wallet {n}',uid,f'w{n}'); rdb.set_reseller_status(self.r['id'],'active')
        kid,secret=rdb.create_api_key(self.r['id'],'test'); p=authenticate_credentials(kid,secret,'test')
        self.ctx=ApiRequestContext('%032x'%n,p.environment,p.reseller_id,p.reseller_public_id,p.api_key_id,p.api_key_public_id)
    def test_credit_debit_refund_balance_and_ledger(self):
        a=credit_wallet(self.r['id'],'10.00','topup:1'); self.assertEqual(a.balance_cents,1000)
        b=debit_wallet(self.r['id'],'8.00','order:1',related_order_reference='order:1'); self.assertEqual(b.balance_cents,200)
        c=refund_wallet(self.r['id'],'8.00','order:1'); self.assertEqual(c.balance_cents,1000)
        self.assertEqual(get_balance(self.r['id'])['balance'],'10.00')
        rows=rdb.list_wallet_transactions(self.r['id']); self.assertEqual(len(rows),3)
        self.assertEqual(rows[0]['currency'],'USDT'); self.assertEqual(rows[0]['related_order_reference'],'order:1')
    def test_idempotency(self):
        a=credit_wallet(self.r['id'],'5','same'); b=credit_wallet(self.r['id'],'5','same')
        self.assertEqual(b.status,'duplicate'); self.assertEqual(a.transaction_id,b.transaction_id)
        self.assertEqual(get_balance(self.r['id'])['balance_cents'],500)
    def test_insufficient_does_not_partially_debit(self):
        credit_wallet(self.r['id'],'10','fund')
        with self.assertRaises(WalletError) as cm: debit_wallet(self.r['id'],'11','order:2')
        self.assertEqual(cm.exception.code,'insufficient_funds'); self.assertEqual(get_balance(self.r['id'])['balance_cents'],1000)
        self.assertIsNone(rdb.get_wallet_transaction_by_reference(self.r['id'],'purchase_debit','order:2'))
    def test_invalid_amounts(self):
        for value in ('0','-1','bad',1.1,True,None):
            with self.subTest(value=value), self.assertRaises(WalletError): credit_wallet(self.r['id'],value,'badref')
    def test_refund_validation(self):
        with self.assertRaises(WalletError): refund_wallet(self.r['id'],'1','missing')
        credit_wallet(self.r['id'],'5','fund2'); debit_wallet(self.r['id'],'2','order:3')
        with self.assertRaises(WalletError): refund_wallet(self.r['id'],'1','order:3')
    def test_rollback_if_ledger_insert_fails(self):
        base=rdb.get_cursor()
        class Failing:
            def execute(self,sql,params=()):
                if sql.strip().upper().startswith('INSERT INTO RESELLER_WALLET_TRANSACTIONS'): raise RuntimeError('forced ledger failure')
                return base.execute(sql,params)
            def __getattr__(self,n): return getattr(base,n)
        with patch.object(rdb,'get_cursor',return_value=Failing()):
            with self.assertRaises(RuntimeError): credit_wallet(self.r['id'],'4','rollback')
        self.assertEqual(get_balance(self.r['id'])['balance_cents'],0)
        self.assertIsNone(rdb.get_wallet_transaction_by_reference(self.r['id'],'topup_credit','rollback'))
    def test_ledger_is_database_immutable(self):
        a=credit_wallet(self.r['id'],'1','immutable')
        with self.assertRaises(Exception):
            db.cur.execute('UPDATE reseller_wallet_transactions SET amount_cents=2 WHERE id=?',(a.transaction_id,))
        db.conn.rollback()
    def test_authenticated_context_owns_wallet(self):
        credit_wallet(self.r['id'],'3','own')
        self.assertEqual(get_authenticated_balance(self.ctx,'test')['balance_cents'],300)
        bad=ApiRequestContext(self.ctx.request_id,'production',self.ctx.reseller_id,self.ctx.reseller_public_id,self.ctx.api_key_id,self.ctx.api_key_public_id)
        with self.assertRaises(WalletError) as cm: get_authenticated_balance(bad,'test')
        self.assertEqual(cm.exception.code,'authorization_denied')
    def test_concurrent_debits_cannot_overdraw(self):
        credit_wallet(self.r['id'],'10','race-fund')
        barrier=threading.Barrier(2); results=[]; lock=threading.Lock()
        def charge(reference):
            barrier.wait()
            try: value=debit_wallet(self.r['id'],'8',reference).status
            except WalletError as exc: value=exc.code
            with lock: results.append(value)
            db.close_thread_connection()
        threads=[threading.Thread(target=charge,args=(f'race-{i}',)) for i in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertCountEqual(results,['posted','insufficient_funds'])
        self.assertEqual(get_balance(self.r['id'])['balance_cents'],200)
    @classmethod
    def tearDownClass(cls):
        db.close_thread_connection()
        try: os.unlink(path)
        except FileNotFoundError: pass
if __name__=='__main__': unittest.main()
