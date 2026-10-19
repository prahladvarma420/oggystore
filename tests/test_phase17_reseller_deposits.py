"""17.1: trusted observations, real atomic settlement, ledger and cross-wallet races.

SQLite runs on isolated temporary database. PostgreSQL integration is explicitly
skipped unless a dedicated disposable backend is provided by the test runner.
No live provider/payment operations are performed.
"""
import asyncio,json,threading,unittest,os
# These fixture-backed unit tests are SQLite-only. Never inherit a live database
# or Telegram token from a developer's shell/.env. A future PostgreSQL integration
# harness must supply a separately owned disposable backend, not production.
os.environ['DATABASE_URL']=''
os.environ['BOT_TOKEN']=''
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock
import tests.test_phase16_admin_fail_order as fixtures
from database import pgcompat
from payments import reseller_deposits as service,deposit_settlement as shared
from payments import trc20_checker as trc,binance_checker as binance,bep20_checker as bep
from utils.timezone import now,now_text
from reseller_api.services.reconciliation import reconcile_reseller
from reseller_api.services.statement_service import generate_statement

db,rdb=fixtures.db,fixtures.rdb
_counter=0
class ResellerDepositTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Imported temporary fixture may have been removed by a prior module.
        # Rebuild only this test-owned database, never a configured production DB.
        db.init_db()
        fixtures.Phase16AdminFailOrderTests.setUpClass()
    @classmethod
    def tearDownClass(cls):fixtures.Phase16AdminFailOrderTests.tearDownClass()
    def setUp(self):
        global _counter
        _counter+=1;self.uid=99817000+_counter
        db.ensure_user_id(self.uid)
        self.rs=rdb.create_reseller('Deposit test',self.uid,'deposit-test');self.rid=self.rs['id'];rdb.set_reseller_status(self.rid,'active')
        for target,value in [('config.USDT_BEP20_ADDRESS','0x'+'1'*40),('config.USDT_TRC20_ADDRESS','T'+'1'*33),('config.BINANCE_PAY_ID','123456789')]:
            p=patch(target,value);p.start();self.addCleanup(p.stop)
        p=patch.object(service,'rails',return_value=[(m,m) for m in shared.METHODS]);p.start();self.addCleanup(p.stop)
        self.context=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()),bot_data={})
    def deposit(self,method='trc20',amount='10.00',uid=None):return service.create_session(uid or self.uid,method,amount)
    def tx(self,method='trc20',n=None):
        # Test-owned namespace prevents a second-payment ID from colliding
        # with the next test's reseller ID in reversed execution order.
        return ('0x' if method=='bep20' else '')+f'{self.uid:08x}{(n or 0):056x}' if method!='binance' else f'BINANCE-{self.uid}-{n or 0}'
    def credit(self,d,tx=None,paid=None):
        return service.credit_verified(d['id'],d['method'],tx or self.tx(d['method']),d['expected_amount'] if paid is None else paid,address=d['address'],notes=d['memo'] or '')
    def scalar(self,sql,*args):db.cur.execute(sql,args);return db.cur.fetchone()[0]
    def assert_clean(self):self.assertEqual(reconcile_reseller(self.rid),[])
    def customer(self,method='trc20',expected=None):
        uid=self.uid+1000000;db.ensure_user_id(uid)
        depid,amt,memo=shared.customer_invoice(uid,10,method)
        if expected is not None:db.cur.execute('UPDATE deposits SET unique_amount=? WHERE id=?',(expected,depid));db.conn.commit();amt=expected
        return uid,depid,amt,memo
    def race(self,functions):
        barrier=threading.Barrier(len(functions));results=[];errors=[]
        def run(fn):
            try:barrier.wait(timeout=10);results.append(fn())
            except Exception as exc:errors.append(type(exc).__name__)
            finally:db.close_thread_connection()
        threads=[threading.Thread(target=run,args=(fn,)) for fn in functions]
        for t in threads:t.start()
        for t in threads:t.join(timeout=20)
        self.assertFalse(any(t.is_alive() for t in threads));self.assertEqual(errors,[])
        return results
    def test_valid_all_rails_and_reference(self):
        for i,method in enumerate(shared.METHODS):
            d=self.deposit(method);result=self.credit(d);self.assertEqual(result['result'],'posted')
            self.assertEqual(service.row(d['id'])['status'],'paid')
            self.assertEqual(rdb.get_wallet_balance_cents(self.rid),1000*(i+1))
            self.assertEqual(self.scalar('SELECT reference FROM reseller_wallet_transactions WHERE id=?',result['deposit']['ledger_transaction_id']),f'dep.{method}.{self.tx(method)}')
        self.assert_clean()
    def test_repeat_is_exactly_once_and_one_event(self):
        d=self.deposit();self.credit(d);self.assertEqual(self.credit(d)['result'],'duplicate')
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),1000)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM reseller_security_events WHERE reseller_id=? AND event_type='reseller_deposit_credited'",self.rid),1);self.assert_clean()
    def test_concurrent_same_deposit(self):
        d=self.deposit();out=self.race([lambda:self.credit(d)]*4)
        self.assertEqual(sum(v['result']=='posted' for v in out),1);self.assertEqual(rdb.get_wallet_balance_cents(self.rid),1000);self.assert_clean()
    def test_transaction_cannot_credit_two_resellers(self):
        d=self.deposit();uid=self.uid+2000000;db.ensure_user_id(uid);rs=rdb.create_reseller('Other',uid,'other');rdb.set_reseller_status(rs['id'],'active');other=self.deposit(uid=uid)
        self.assertEqual(self.credit(d)['result'],'posted');self.assertEqual(self.credit(other,tx=self.tx())['result'],'payment_used');self.assertEqual(rdb.get_wallet_balance_cents(rs['id']),0)
    def test_customer_then_reseller_collision(self):
        d=self.deposit();uid,cid,amount,memo=self.customer();tx=self.tx()
        self.assertTrue(shared.settle_customer(cid,'trc20',tx,amount)['posted']);self.assertEqual(self.credit(d)['result'],'payment_used');self.assertEqual(db.get_balance(uid),10);self.assert_clean()
    def test_reseller_then_customer_collision(self):
        d=self.deposit();uid,cid,amount,memo=self.customer();self.credit(d)
        self.assertFalse(shared.settle_customer(cid,'trc20',self.tx(),amount)['posted']);self.assertEqual(db.get_balance(uid),0);self.assert_clean()
    def test_cross_table_concurrent_claim_one_winner(self):
        d=self.deposit();uid,cid,amount,memo=self.customer()
        # Distinct invoice amounts; deliberately reuse the TX ID to race the global claim.
        results=self.race([lambda:self.credit(d),lambda:shared.settle_customer(cid,'trc20',self.tx(),amount)])
        self.assertEqual(sum(v.get('posted',v.get('result')=='posted') for v in results),1)
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid)+round(db.get_balance(uid)*100),1000);self.assert_clean()
    def test_atomic_security_failure_and_resume(self):
        d=self.deposit()
        original=rdb.log_security_event
        def fail(*a,**k):original(*a,**k);raise RuntimeError('synthetic post-insert failure')
        with patch.object(rdb,'log_security_event',side_effect=fail):
            with self.assertRaises(RuntimeError):self.credit(d)
        self.assertEqual(service.row(d['id'])['status'],'pending');self.assertEqual(rdb.get_wallet_balance_cents(self.rid),0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM processed_txs WHERE tx_hash=?',self.tx()),0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM reseller_security_events WHERE reseller_id=? AND event_type='reseller_deposit_credited'",self.rid),0)
        self.assertEqual(self.credit(d)['result'],'posted');self.assert_clean()
    def test_ledger_exception_rolls_back_claim(self):
        d=self.deposit()
        with patch.object(rdb,'post_wallet_transaction',side_effect=RuntimeError('synthetic unavailable ledger')):
            with self.assertRaises(RuntimeError):self.credit(d)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM processed_txs WHERE tx_hash=?',self.tx()),0)
        self.assertEqual(self.credit(d)['result'],'posted')
    def test_expired_no_automatic_late_credit(self):
        d=self.deposit();db.cur.execute("UPDATE reseller_deposits SET expires_at='2000-01-01 00:00:00' WHERE id=?",(d['id'],));db.conn.commit()
        self.assertEqual(self.credit(d)['result'],'late_review');self.assertEqual(service.row(d['id'])['status'],'expired');self.assertEqual(rdb.get_wallet_balance_cents(self.rid),0)
    def test_exact_chain_amount_tolerance_and_no_partial_aggregation(self):
        d=self.deposit();expected=Decimal(str(d['expected_amount']))
        self.assertEqual(self.credit(d,paid=expected-Decimal('.01'))['result'],'unmatched')
        self.assertEqual(self.credit(d,paid=expected+Decimal('.01'))['result'],'unmatched')
        self.assertEqual(self.credit(d,paid=expected/2)['result'],'unmatched')
        self.assertEqual(self.credit(d,paid=expected-Decimal('.000001'))['result'],'posted');self.assert_clean()
    def test_binance_overpayment_capped_and_underpayment_rejected(self):
        d=self.deposit('binance');self.assertEqual(self.credit(d,paid='9.9')['result'],'unmatched')
        self.assertEqual(self.credit(d,paid='15')['result'],'posted');self.assertEqual(rdb.get_wallet_balance_cents(self.rid),1000)
    def test_suspended_receives_eligible_credit(self):
        d=self.deposit();rdb.set_reseller_status(self.rid,'suspended');self.assertEqual(self.credit(d)['result'],'posted')
        desc=self.scalar("SELECT description FROM reseller_security_events WHERE reseller_id=? AND event_type='reseller_deposit_credited'",self.rid)
        self.assertTrue(json.loads(desc)['suspended']);self.assert_clean()
    def test_legacy_paid_customer_without_marker_blocks_credit(self):
        d=self.deposit();uid,cid,_,_=self.customer();tx=self.tx().upper();db.cur.execute("UPDATE deposits SET status='paid',tx_hash=? WHERE id=?",(tx,cid));db.conn.commit()
        self.assertEqual(self.credit(d)['result'],'payment_used');self.assertEqual(rdb.get_wallet_balance_cents(self.rid),0)
    def test_customer_concurrent_duplicates_fixed(self):
        uid,cid,amt,_=self.customer();result=self.race([lambda:shared.settle_customer(cid,'trc20',self.tx(),amt)]*4)
        self.assertEqual(sum(v['posted'] for v in result),1);self.assertEqual(db.get_balance(uid),10)
    def test_customer_checkout_stock_and_order_roll_back(self):
        from utils.crypto import decrypt_text
        uid,cid,amt,_=self.customer();db.cur.execute("INSERT INTO products(name,price,stock,delivery,active) VALUES('Checkout deposit',10,1,'code_stock',1)");pid=db.cur.lastrowid
        db.cur.execute('INSERT INTO product_stock(product_id,delivery,sold) VALUES(?,?,0)',(pid,'SYNTHETIC-CODE'))
        db.cur.execute('UPDATE deposits SET checkout_product_id=?,checkout_quantity=1 WHERE id=?',(pid,cid));db.conn.commit()
        with patch('utils.crypto.encrypt_text',side_effect=RuntimeError('synthetic encryption failure')):
            with self.assertRaises(RuntimeError):shared.settle_customer(cid,'trc20',self.tx(),amt)
        self.assertEqual(self.scalar('SELECT sold FROM product_stock WHERE product_id=?',pid),0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM processed_txs WHERE tx_hash=?',self.tx()),0)
        v=shared.settle_customer(cid,'trc20',self.tx(),amt);self.assertTrue(v['posted']);self.assertEqual(decrypt_text(self.scalar('SELECT delivery FROM orders WHERE id=?',v['order_id'])),'SYNTHETIC-CODE');self.assertEqual(db.get_balance(uid),0)
    def test_customer_balance_only_checkout_fallback_remains_wallet_only(self):
        import tempfile,os
        # Independent disposable DB is needed: no reseller evidence must exist.
        fd,path=tempfile.mkstemp(prefix='phase17_customer_fallback_');os.close(fd)
        old_path,old_tls=db.DB_PATH,db._tls
        try:
            db.DB_PATH=path;db._tls=threading.local();db.init_db()
            uid=self.uid;db.ensure_user_id(uid);cid,amt,_=shared.customer_invoice(uid,10,'bep20')
            db.cur.execute("INSERT INTO products(name,price,stock,delivery,active) VALUES('Fallback checkout',10,1,'code_stock',1)");pid=db.cur.lastrowid
            db.cur.execute('INSERT INTO product_stock(product_id,delivery,sold) VALUES(?,?,0)',(pid,'FALLBACK-NOT-DELIVERED'))
            db.cur.execute('UPDATE deposits SET checkout_product_id=?,checkout_quantity=1 WHERE id=?',(pid,cid));db.conn.commit()
            result=shared.settle_customer(cid,'bep20',f'balance-delta-{cid}',amt,fallback=True)
            self.assertTrue(result['posted']);self.assertEqual(db.get_balance(uid),10)
            self.assertEqual(self.scalar('SELECT sold FROM product_stock WHERE product_id=?',pid),0)
            self.assertEqual(self.scalar('SELECT COUNT(*) FROM orders'),0)
        finally:
            db.close_thread_connection();db.DB_PATH=old_path;db._tls=old_tls
            for suffix in ('','-wal','-shm'):
                try:os.unlink(path+suffix)
                except FileNotFoundError:pass
    def test_balance_only_fallback_holds_when_reseller_address_shared(self):
        self.deposit('bep20');uid,cid,amt,_=self.customer('bep20')
        v=shared.settle_customer(cid,'bep20',f'balance-delta-{cid}',amt,fallback=True)
        self.assertTrue(v['review']);self.assertEqual(db.get_balance(uid),0)
    def test_wallet_surfaces_and_balance_chain(self):
        d=self.deposit();self.credit(d);d2=self.deposit();self.credit(d2,tx=self.tx(n=self.uid+1));self.assert_clean()
        db.cur.execute('SELECT amount_cents,balance_after_cents FROM reseller_wallet_transactions WHERE reseller_id=? ORDER BY id',(self.rid,));balance=0
        for amt,after in db.cur.fetchall():balance+=amt;self.assertEqual(balance,after)
        key,secret=rdb.create_api_key(self.rid,'test');headers=fixtures._hdr(key,secret)
        summary=fixtures.Phase16AdminFailOrderTests.client.get('/api/v1/wallet/summary',headers=headers);self.assertEqual(summary.status_code,200)
        self.assertIn('20.00',json.dumps(summary.get_json()))
        history=fixtures.Phase16AdminFailOrderTests.client.get('/api/v1/transactions',headers=headers);self.assertEqual(history.status_code,200);self.assertEqual(len(history.get_json()['data']['transactions']),2)
        st=generate_statement(self.rid,period_type='monthly',month=now().strftime('%Y-%m'));self.assertEqual(st['topup_cents'],2000);self.assertEqual(st['closing_balance_cents'],2000)
        owner=fixtures.Phase16AdminFailOrderTests('test_successful_fail_full_effects')._owner();result=owner.get('/api/financial/overview');self.assertEqual(result.status_code,200)
    def test_trc_checker_real_boundary_and_notifications_after_commit(self):
        d=self.deposit();
        async def notify(**kw):self.assertFalse(db.get_conn().in_transaction)
        self.context.bot.send_message=AsyncMock(side_effect=notify)
        with patch.object(trc,'_config_ready',return_value=True),patch.object(trc,'_recent_incoming_transfers',new=AsyncMock(return_value=[(self.tx(),Decimal(str(d['expected_amount'])))])):
            asyncio.run(trc.check_trc20_payments(self.context));asyncio.run(trc.check_trc20_payments(self.context))
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),1000);self.assertEqual(sum(v.kwargs.get("chat_id")==self.uid for v in self.context.bot.send_message.await_args_list),1)
    def test_binance_actual_checker_asset_allowlist(self):
        d=self.deposit('binance');good={'transactionId':self.tx('binance'),'amount':'12','currency':'USDT','note':d['memo']}
        bad=dict(good,transactionId='BAD-'+self.tx('binance'),currency='USDC')
        with patch.object(binance,'_config_ready',return_value=True),patch.object(binance,'_recent_pay_transactions',new=AsyncMock(return_value=[bad,good])):
            asyncio.run(binance.check_binance_payments(self.context));asyncio.run(binance.check_binance_payments(self.context))
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),1000);self.assertEqual(service.row(d['id'])['txid'],good['transactionId']);self.assert_clean()
    def test_trc_provider_validates_destination_asset_and_precision(self):
        address=service.destination('trc20');contract=trc.USDT_TRC20_CONTRACT
        good={'to':address,'token_info':{'address':contract,'decimals':trc.USDT_TRC20_DECIMALS},'value':'10001234','transaction_id':self.tx()}
        response=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'data':[dict(good,to='WRONG'),dict(good,token_info={'address':'WRONG','decimals':6}),dict(good,token_info={'address':contract,'decimals':18}),good]})
        http=AsyncMock();http.get.return_value=response;http.__aenter__.return_value=http
        with patch.object(trc,'USDT_TRC20_ADDRESS',address),patch.object(trc.httpx,'AsyncClient',return_value=http):
            result=asyncio.run(trc._recent_incoming_transfers())
        self.assertEqual(result,[(self.tx(),Decimal('10.001234'))]);self.assertEqual(http.get.call_args.kwargs['params']['only_confirmed'],'true')
    def test_bep_provider_filter_contract_confirmation_destination(self):
        addr=service.destination('bep20');contract=bep.USDT_BEP20_CONTRACT
        good={'address':contract,'topics':[bep.TRANSFER_TOPIC,'0x'+'0'*64,bep._topic_address(addr)],'data':hex(10*10**bep.USDT_DECIMALS),'blockNumber':95,'blockHash':'0x'+f'{95:064x}','logIndex':0,'transactionHash':self.tx('bep20')}
        calls=[]
        def logs(query):calls.append(query);return [dict(good,address='0x'+'2'*40),dict(good,removed=True),dict(good,blockNumber=99),dict(good,topics=[bep.TRANSFER_TOPIC,'0x'+'0'*64,bep._topic_address('0x'+'3'*40)]),good]
        w3=SimpleNamespace(eth=SimpleNamespace(block_number=100,get_logs=logs,get_transaction_receipt=lambda tx:{'status':1,'transactionHash':good['transactionHash'],'blockNumber':95,'blockHash':good['blockHash'],'logs':[good]},get_block=lambda height:{'hash':'0x'+f'{height:064x}'}))
        with patch.object(bep,'USDT_BEP20_ADDRESS',addr),patch.object(bep,'_load_last_scanned_block',return_value=90):result,scanned=bep._get_recent_incoming_usdt_logs(self.context,w3)
        self.assertEqual(result,[good]);self.assertEqual(scanned,95);self.assertEqual(calls[0]['toBlock'],100-bep.PAYMENT_REQUIRED_CONFIRMATIONS)
    def test_bep_actual_checker_credits_reseller_from_verified_logs(self):
        d=self.deposit('bep20');addr=d['address'];contract=bep.USDT_BEP20_CONTRACT
        good={'address':contract,'topics':[bep.TRANSFER_TOPIC,'0x'+'0'*64,bep._topic_address(addr)],'data':hex(int(Decimal(str(d['expected_amount']))*10**bep.USDT_DECIMALS)),'blockNumber':95,'blockHash':'0x'+f'{95:064x}','logIndex':0,'transactionHash':self.tx('bep20')}
        w3=SimpleNamespace(eth=SimpleNamespace(block_number=100,get_logs=lambda query:[good],get_transaction_receipt=lambda tx:{'status':1,'transactionHash':good['transactionHash'],'blockNumber':95,'blockHash':good['blockHash'],'logs':[good]},get_block=lambda height:{'hash':'0x'+f'{height:064x}'}))
        self.context.bot_data['last_wallet_balance']=Decimal('0')
        with patch.object(bep,'_config_ready',return_value=True),patch.object(bep,'USDT_BEP20_ADDRESS',addr),patch.object(bep,'_get_web3',return_value=w3),patch.object(bep,'_wallet_usdt_balance',return_value=Decimal(str(d['expected_amount']))),patch.object(bep,'_load_last_scanned_block',return_value=90):
            asyncio.run(bep.check_payments(self.context));asyncio.run(bep.check_payments(self.context))
        self.assertEqual(deposits_row:=service.row(d['id'])['status'],'paid');self.assertEqual(rdb.get_wallet_balance_cents(self.rid),1000)
        self.assertEqual(sum(v.kwargs.get('chat_id')==self.uid for v in self.context.bot.send_message.await_args_list),1);self.assert_clean()
    def test_wrong_network_rpc_is_rejected(self):
        from unittest.mock import MagicMock
        w3=MagicMock();w3.eth.chain_id=1;w3.eth.block_number=100;w3.is_connected.return_value=True
        with patch.object(bep.Web3,'HTTPProvider'),patch.object(bep,'Web3',return_value=w3),patch.object(bep,'BACKUP_RPC_URLS',[]):
            with self.assertRaises(RuntimeError):bep._get_web3(self.context)
    def test_scanner_failure_does_not_advance_reseller_processing_result(self):
        d=self.deposit()
        with patch.object(service,'credit_verified',side_effect=RuntimeError('synthetic commit failure')):
            ok=asyncio.run(service.process_observations(self.context,'trc20',[(self.tx(),d['expected_amount'],'')]))
        self.assertFalse(ok);self.assertEqual(service.row(d['id'])['status'],'pending')
        self.assertTrue(asyncio.run(service.process_observations(self.context,'trc20',[(self.tx(),d['expected_amount'],'')])));self.assert_clean()
    def test_owner_action_sink_commits_and_releases_connection(self):
        import web_admin.app as wa
        from database.pgcompat import atomic
        owner=fixtures.Phase16AdminFailOrderTests('test_successful_fail_full_effects')._owner()
        with wa.app.test_request_context('/api/test'):
            from flask import session
            session['username']='owner';wa.log_action('deposit-regression','synthetic only')
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM admin_logs WHERE action='[WEB] deposit-regression'"),1)
        # A leaked WebAdmin writer would make this real independent write lock.
        with atomic(db.get_conn()):db.cur.execute("INSERT INTO settings(key,value) VALUES(?,?)",(f'test.deposit.audit.{self.uid}','1'))
    def test_schema_and_postgres_translation(self):
        db.cur.execute('PRAGMA index_list(reseller_deposits)');indexes={v[1]:v[2] for v in db.cur.fetchall()};self.assertEqual(indexes['uq_reseller_deposits_tx'],1)
        self.assertIn('reseller_deposits',pgcompat.ID_TABLES)
        sql,returning=pgcompat._translate('INSERT INTO reseller_deposits(reseller_id) VALUES(?)');self.assertIn('RETURNING id',sql);self.assertTrue(returning)
        rdb.ensure_reseller_deposit_table();self.assertEqual(service.row(self.deposit()['id'])['status'],'pending')
    @unittest.skip('No disposable PostgreSQL server available; translation checks are not live backend proof.')
    def test_live_postgresql_settlement(self):pass

if __name__=='__main__':unittest.main()
