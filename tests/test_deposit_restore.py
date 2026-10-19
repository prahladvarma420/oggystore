"""Invoice restoration: real SQLite settlement, external RPC/Telegram mocked."""
import os
os.environ['DATABASE_URL']='';os.environ['BOT_TOKEN']=''
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch
import tests.test_customer_deposit_regression as fixtures
from payments import bep20_checker as bep,deposit_settlement as shared,reseller_deposits as reseller
from handlers import reseller_handlers as ui
from utils.states import user_state,RES_TOPUP_AMOUNT
from database import db,reseller_db as rdb

class DepositRestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixtures.CustomerDepositRegressionTests.setUpClass()
    @classmethod
    def tearDownClass(cls):fixtures.CustomerDepositRegressionTests.tearDownClass()
    def setUp(self):
        self.fx=fixtures.CustomerDepositRegressionTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.h=self.fx.helper;self.context=self.fx.context
        self.addCleanup(user_state.pop,self.h.uid,None);self.addCleanup(ui._topup_drafts.pop,self.h.uid,None)
    def test_checkpoint_never_skips_unprocessed_blocks_after_outage(self):
        seen=[];rpc=SimpleNamespace(eth=SimpleNamespace(block_number=12000,get_logs=lambda q:seen.append(q) or []))
        with patch.object(bep,'_load_last_scanned_block',return_value=100),patch.object(bep,'USDT_BEP20_ADDRESS',reseller.destination('bep20')):
            logs,end=bep._get_recent_incoming_usdt_logs(self.context,rpc)
        self.assertEqual(seen[0]['fromBlock'],101)
        self.assertEqual(end,100+bep.LOG_SCAN_CHUNK_BLOCKS*bep.MAX_CHUNKS_PER_CYCLE);self.assertEqual(logs,[])
    def test_failing_rpc_rotates_to_backup_not_same_primary(self):
        a=SimpleNamespace(eth=SimpleNamespace(block_number=100,chain_id=56),is_connected=lambda:True)
        b=SimpleNamespace(eth=SimpleNamespace(block_number=100,chain_id=56),is_connected=lambda:True)
        factory=Mock(side_effect=lambda url:{'https://primary.invalid':a,'https://backup.invalid':b}[url])
        factory.HTTPProvider=Mock(side_effect=lambda url,**kwargs:url)
        with patch.object(bep,'Web3',factory),patch.object(bep,'RPC_URL','https://primary.invalid'),patch.object(bep,'BACKUP_RPC_URLS',['https://backup.invalid']):
            self.assertIs(bep._get_web3(self.context),a);bep._drop_cached_web3(self.context)
            self.assertIs(bep._get_web3(self.context),b)
    def test_fresh_scan_keeps_bounded_initial_window(self):
        seen=[];rpc=SimpleNamespace(eth=SimpleNamespace(block_number=12000,get_logs=lambda q:seen.append(q) or []))
        with patch.object(bep,'_load_last_scanned_block',return_value=0),patch.object(bep,'USDT_BEP20_ADDRESS',reseller.destination('bep20')):
            bep._get_recent_incoming_usdt_logs(self.context,rpc)
        self.assertEqual(seen[0]['fromBlock'],12000-bep.PAYMENT_REQUIRED_CONFIRMATIONS-bep.PAYMENT_SCAN_BLOCKS)
    def test_old_flag_cannot_hijack_invoice_flow(self):
        q=SimpleNamespace(message=SimpleNamespace(reply_photo=AsyncMock()))
        with patch.dict(os.environ,RESELLER_BEP20_UNIQUE_ENABLED='1'),patch.object(ui,'edit_or_send',new=AsyncMock()) as edit:
            asyncio.run(ui._handle_reseller_topup_callback(q,self.h.uid,'res_topup_method_bep20'))
        self.assertEqual(user_state[self.h.uid],RES_TOPUP_AMOUNT)
        self.assertEqual(ui._topup_drafts[self.h.uid],'bep20');self.assertIn('Send an amount',edit.call_args.args[1])
    def test_customer_database_failure_retries_before_checkpoint(self):
        uid,cid,amount,_=self.fx.customer('bep20');stack,_=self.fx.bep_feed(amount)
        db.cur.execute("CREATE TEMP TRIGGER restore_fail_paid BEFORE UPDATE OF status ON deposits WHEN NEW.id="+str(cid)+" AND NEW.status='paid' BEGIN SELECT RAISE(ABORT,'test-only failure'); END");db.conn.commit()
        try:
            with stack:asyncio.run(bep.check_payments(self.context))
            self.assertNotIn('last_scanned_block',self.context.bot_data);self.assertEqual(db.get_balance(uid),0)
        finally:db.cur.execute('DROP TRIGGER restore_fail_paid');db.conn.commit()
        stack,_=self.fx.bep_feed(amount)
        with stack:asyncio.run(bep.check_payments(self.context))
        self.fx.assert_customer_only(uid,cid)
    def test_reseller_bep_checker_only_credits_reseller_wallet(self):
        d=self.h.deposit('bep20');stack,_=self.fx.bep_feed(d['expected_amount'])
        with stack:
            asyncio.run(bep.check_payments(self.context));asyncio.run(bep.check_payments(self.context))
        self.assertEqual(reseller.row(d['id'])['status'],'paid');self.assertEqual(rdb.get_wallet_balance_cents(self.h.rid),1000)
        self.assertEqual(db.get_balance(self.h.uid),0);self.h.assert_clean()

    def test_failed_or_noncanonical_receipt_never_credits(self):
        for problem in ('failed','block','log','recipient'):
            with self.subTest(problem=problem):
                uid,cid,amount,_=self.fx.customer('bep20');stack,entry=self.fx.bep_feed(amount)
                receipt={'status':1,'transactionHash':entry['transactionHash'],'blockNumber':95,'blockHash':entry['blockHash'],'logs':[entry]}
                if problem=='failed':receipt['status']=0
                if problem=='block':receipt['blockHash']='0x'+'f'*64
                if problem=='log':receipt['logs']=[]
                if problem=='recipient':receipt['logs']=[dict(entry,topics=[bep.TRANSFER_TOPIC,'0x'+'0'*64,bep._topic_address('0x'+'3'*40)])]
                with stack:
                    rpc=bep._get_web3(self.context);rpc.eth.get_transaction_receipt=lambda tx:receipt
                    asyncio.run(bep.check_payments(self.context))
                self.assertEqual(db.get_balance(uid),0)
                self.assertEqual(self.h.scalar('SELECT status FROM deposits WHERE id=?',cid),'pending')
    def test_receipt_outage_keeps_checkpoint_and_retry_recovers(self):
        uid,cid,amount,_=self.fx.customer('bep20');stack,_=self.fx.bep_feed(amount)
        with stack:
            rpc=bep._get_web3(self.context)
            rpc.eth.get_transaction_receipt=Mock(side_effect=TimeoutError('test-only-rpc-private-url'))
            asyncio.run(bep.check_payments(self.context))
        self.assertEqual(db.get_balance(uid),0);self.assertNotIn('last_scanned_block',self.context.bot_data)
        stack,_=self.fx.bep_feed(amount)
        with stack:asyncio.run(bep.check_payments(self.context))
        self.fx.assert_customer_only(uid,cid)
    def test_balance_probe_without_transaction_is_never_automatic_payment(self):
        uid,cid,amount,_=self.fx.customer('bep20');stack,_=self.fx.bep_feed(amount,entries=[])
        with stack,patch.object(bep,'settle_customer',wraps=shared.settle_customer_safely) as settle:
            asyncio.run(bep.check_payments(self.context))
        self.assertEqual(db.get_balance(uid),0);self.assertFalse(settle.called)
    def test_restored_bootstrap_does_not_create_retired_tables(self):
        rdb.ensure_reseller_tables()
        self.assertEqual(self.h.scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'reseller_bep20_%'"),0)
    def test_historical_reservation_still_blocks_invoice_replay(self):
        uid,cid,amount,_=self.fx.customer('bep20');tx=self.h.tx('bep20')
        db.cur.execute('CREATE TABLE reseller_bep20_claims(tx_hash TEXT PRIMARY KEY,chain_id INTEGER,created_at TEXT)');db.conn.commit()
        try:
            db.cur.execute("INSERT INTO reseller_bep20_claims VALUES(?,56,'2000-01-01')",(tx,));db.conn.commit()
            self.assertFalse(shared.settle_customer(cid,'bep20',tx,amount)['posted'])
            self.assertEqual(db.get_balance(uid),0)
        finally:db.cur.execute('DROP TABLE reseller_bep20_claims');db.conn.commit()

    def test_stale_address_callback_has_no_address_and_revalidates_access(self):
        q=SimpleNamespace(message=SimpleNamespace(reply_photo=AsyncMock()))
        with patch.object(ui,'edit_or_send',new=AsyncMock()) as edit:
            asyncio.run(ui._handle_reseller_topup_callback(q,self.h.uid,'res_topup_unique_bep20'))
        self.assertIn('Unknown top-up action',edit.call_args.args[1]);self.assertFalse(q.message.reply_photo.called)
        rdb.set_reseller_status(self.h.rid,'suspended')
        with patch.object(ui,'edit_or_send',new=AsyncMock()) as edit:
            asyncio.run(ui._handle_reseller_topup_callback(q,self.h.uid,'res_topup_method_bep20'))
        self.assertNotIn(self.h.uid,ui._topup_drafts);self.assertNotIn(self.h.uid,user_state)
    def test_historical_accounted_event_cannot_replay_without_claim_table(self):
        uid,cid,amount,_=self.fx.customer('bep20');tx=self.h.tx('bep20')
        db.cur.execute('CREATE TABLE reseller_bep20_events(tx_hash TEXT,state TEXT)');db.conn.commit()
        try:
            db.cur.execute("INSERT INTO reseller_bep20_events VALUES(?,'accounted')",(tx,));db.conn.commit()
            self.assertFalse(shared.settle_customer(cid,'bep20',tx,amount)['posted']);self.assertEqual(db.get_balance(uid),0)
        finally:db.cur.execute('DROP TABLE reseller_bep20_events');db.conn.commit()

if __name__=='__main__':unittest.main()
