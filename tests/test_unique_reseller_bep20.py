"""New permanent-address core: SQLite integration, RPC/custody boundaries mocked.

No provider account, live blockchain transfer, production funds or secrets are used.
Native PostgreSQL integration is explicitly separate and not inferred from SQLite.
"""
import os
os.environ['DATABASE_URL']=''
os.environ['BOT_TOKEN']=''
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch, AsyncMock
import tests.test_phase17_reseller_deposits as fixtures
from payments import unique_bep20 as core
from payments import deposit_settlement as legacy
from database import db, reseller_db as rdb
from reseller_api.workers import reseller_deposit_worker as worker
from handlers import reseller_handlers as ui

TOKEN='0x'+'a'*40


class Custody:
    """TEST ONLY: deterministic external boundary, never production selection."""
    def __init__(self, addresses):
        self.addresses=addresses; self.status='accepted'
    def inventory(self,cfg):
        return [{'address':a,'provider_ref':'test-address-'+a[2:],
                 'chain_id':56,'wallet_ref':cfg.wallet_ref} for a in self.addresses]
    def acceptance(self,cfg,event,provider_ref):
        return {'status':self.status,'reference':'test-deposit-'+event.key,
                'chain_id':event.chain_id,'token':event.token,'tx_hash':event.tx_hash,
                'log_index':event.log_index,'address':event.recipient,
                'raw_units':str(event.raw_units),'wallet_ref':cfg.wallet_ref}


class RPC:
    def __init__(self,logs):
        self.logs=logs;self.eth=self;self.chain_id=56;self.block_number=200
        self.receipt_status=1;self.blocks={};self.hook=None
    def get_logs(self,query):
        recipients=query['topics'][2]
        return [x for x in self.logs if query['fromBlock']<=x['blockNumber']<=query['toBlock'] and x['topics'][2] in recipients]
    def get_block(self,n):
        return {'hash':self.blocks.get(n,'0x'+f'{n:064x}')}
    def get_transaction_receipt(self,tx):
        logs=[x for x in self.logs if x['transactionHash']==tx]
        return {'status':self.receipt_status,'transactionHash':tx,'blockNumber':logs[0]['blockNumber'],
                'blockHash':logs[0]['blockHash'],'logs':logs}
    def get_code(self,a):return b'\x60\x01'
    def call(self,query):return (18).to_bytes(32,'big')


class UniqueResellerBep20Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixtures.ResellerDepositTests.setUpClass()
    @classmethod
    def tearDownClass(cls):fixtures.ResellerDepositTests.tearDownClass()
    def setUp(self):
        self.h=fixtures.ResellerDepositTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.uid=self.h.uid;self.rid=self.h.rid
        self.cfg=core.Settings(TOKEN,'test-only',f'test-wallet-{self.uid}',90,1,batch_blocks=10)
        self.dest='0x'+f'{self.uid:040x}'
        self.otherdest='0x'+f'{self.uid+1000000000:040x}'
        self.provider=Custody([self.dest,self.otherdest])
        core.import_inventory(self.cfg,self.provider)
        self.saved=core.allocate_address(self.uid,self.cfg)
        self.dest=self.saved['address']
    def scalar(self,sql,*args):return self.h.scalar(sql,*args)
    def log(self,raw=12*10**18,index=0,tx=None,block=95,dest=None):
        return {'address':TOKEN,'topics':[core.TRANSFER,'0x'+'0'*64,'0x'+'0'*24+(dest or self.dest)[2:]],
                'data':'0x'+f'{raw:064x}','transactionHash':tx or self.h.tx('bep20'),
                'logIndex':index,'blockNumber':block,'blockHash':'0x'+f'{block:064x}', 'removed':False}
    def process(self,raw=12*10**18,index=0,tx=None):
        log=self.log(raw,index,tx);return core.process_log(self.cfg,RPC([log]),self.provider,log)
    def clean(self):
        self.assertEqual(core.reconcile_exact(self.rid,self.cfg)['problems'],[])
        self.h.assert_clean()
    def test_permanent_address_and_unique_ownership(self):
        self.assertEqual(core.allocate_address(self.uid,self.cfg),self.saved)
        uid=self.uid+50000000;db.ensure_user_id(uid);other=rdb.create_reseller('Second',uid,'second');rdb.set_reseller_status(other['id'],'active')
        second=core.allocate_address(uid,self.cfg)
        self.assertNotEqual(second['address'],self.dest)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM reseller_bep20_addresses WHERE reseller_id=?',self.rid),1)
    def test_database_rejects_duplicate_assignment(self):
        with self.assertRaises(Exception):
            with fixtures.pgcompat.atomic(db.get_conn()):
                db.get_cursor().execute('UPDATE reseller_bep20_addresses SET reseller_id=? WHERE address!=? AND reseller_id IS NULL',(self.rid,self.dest))
        self.assertEqual(core.allocate_address(self.uid,self.cfg)['address'],self.dest)
    def test_concurrent_reopen_same_assignment(self):
        results=self.h.race([lambda:core.allocate_address(self.uid,self.cfg)]*4)
        self.assertEqual({v['address'] for v in results},{self.dest})
    def test_concurrent_first_allocation(self):
        uid=self.uid+50000000;db.ensure_user_id(uid);rs=rdb.create_reseller('Concurrent',uid,'concurrent');rdb.set_reseller_status(rs['id'],'active')
        results=self.h.race([lambda:core.allocate_address(uid,self.cfg)]*4)
        self.assertEqual(len({v['address'] for v in results}),1)
    def test_inventory_timeout_and_uncertain_request_does_not_allocate(self):
        before=self.scalar('SELECT COUNT(*) FROM reseller_bep20_addresses')
        with patch.object(self.provider,'inventory',side_effect=TimeoutError('secret-provider-url')):
            with self.assertRaisesRegex(core.DepositUnavailable,'Custody inventory unavailable'):
                core.import_inventory(self.cfg,self.provider)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM reseller_bep20_addresses'),before)
        self.assertEqual(core.allocate_address(self.uid,self.cfg)['address'],self.dest)
    def test_inventory_batch_rollback_on_owner_conflict(self):
        extra='0x'+f'{self.uid+9000000000:040x}'
        rows=Custody([extra]).inventory(self.cfg)+[dict(self.provider.inventory(self.cfg)[0],wallet_ref='other')]
        with patch.object(self.provider,'inventory',return_value=rows):
            with self.assertRaises(core.DepositUnavailable):core.import_inventory(self.cfg,self.provider)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM reseller_bep20_addresses WHERE address=?',extra),0)
    def test_legacy_shared_address_is_not_importable(self):
        with patch.object(self.provider,'inventory',return_value=[dict(self.provider.inventory(self.cfg)[0],address='0x'+'1'*40)]):
            with self.assertRaises(core.DepositUnavailable):core.import_inventory(self.cfg,self.provider)
    def test_arbitrary_deposits_exact_fractional_carry(self):
        self.assertEqual(self.process(6*10**15),'retained')
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),0)
        self.assertEqual(self.process(7*10**15,index=1),'credited')
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),1)
        totals=core.reconcile_exact(self.rid,self.cfg)
        self.assertEqual(totals['remainder_units'],str(3*10**15));self.clean()
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM reseller_wallet_transactions WHERE reseller_id=?',self.rid),1)
    def test_multi_log_same_hash_and_multiple_deposits(self):
        for index,amount in enumerate((12*10**18,475*10**17,1)):
            self.process(amount,index=index)
        self.process(10**18,tx=self.h.tx('bep20',8))
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),6050)
        self.assertEqual(core.reconcile_exact(self.rid,self.cfg)['remainder_units'],'1');self.clean()
    def test_multi_recipient_logs_credit_correct_reseller(self):
        uid=self.uid+50000000;db.ensure_user_id(uid);rs=rdb.create_reseller('Second',uid,'second');rdb.set_reseller_status(rs['id'],'active')
        saved=core.allocate_address(uid,self.cfg)
        logs=[self.log(10**18),self.log(2*10**18,index=1,dest=saved['address'])];rpc=RPC(logs)
        for log in logs:core.process_log(self.cfg,rpc,self.provider,log)
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),100)
        self.assertEqual(rdb.get_wallet_balance_cents(rs['id']),200);self.clean()
    def test_receipt_validation_matrix(self):
        cases=[('network',lambda l,r:setattr(r,'chain_id',1)),
               ('token',lambda l,r:l.update(address='0x'+'b'*40)),
               ('failed',lambda l,r:setattr(r,'receipt_status',0)),
               ('confirmations',lambda l,r:setattr(r,'block_number',96)),
               ('removed',lambda l,r:l.update(removed=True)),
               ('canonical',lambda l,r:r.blocks.update({95:'0x'+'f'*64})),
               ('amount',lambda l,r:l.update(data='0x01'))]
        for name,mutate in cases:
            with self.subTest(name=name):
                log=self.log();rpc=RPC([log]);mutate(log,rpc)
                with self.assertRaises(core.DepositUnavailable):core.process_log(self.cfg,rpc,self.provider,log)
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),0)
    def test_mismatched_receipt_log_rejected(self):
        log=self.log();rpc=RPC([log]);wrong=dict(log,data='0x'+f'{1:064x}')
        receipt=rpc.get_transaction_receipt(log['transactionHash']);receipt['logs']=[wrong]
        with patch.object(rpc,'get_transaction_receipt',return_value=receipt):
            with self.assertRaises(core.DepositUnavailable):core.process_log(self.cfg,rpc,self.provider,log)
    def test_custody_pending_then_acceptance(self):
        self.provider.status='pending';self.assertEqual(self.process(),'pending')
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),0)
        self.provider.status='accepted';self.assertEqual(self.process(),'credited');self.clean()
    def test_custody_rejection_and_minimum_are_review_not_credit(self):
        self.provider.status='rejected';self.assertEqual(self.process(),'review')
        self.provider.status='accepted';self.assertEqual(self.process(),'review')
        cfg=replace(self.cfg,minimum_units=10**18)
        log=self.log(1,index=1)
        self.assertEqual(core.process_log(cfg,RPC([log]),self.provider,log),'review')
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),0)
    def test_custody_failure_sanitized_no_partial_state(self):
        with patch.object(self.provider,'acceptance',side_effect=RuntimeError('secret-provider-key')):
            with self.assertRaises(core.DepositUnavailable) as error:self.process()
        self.assertNotIn('secret',str(error.exception))
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM reseller_bep20_events WHERE address=?',self.dest),0)
    def test_custody_identity_mismatch(self):
        original=self.provider.acceptance
        def wrong(*args):return dict(original(*args),raw_units='1')
        with patch.object(self.provider,'acceptance',side_effect=wrong):
            with self.assertRaises(core.DepositUnavailable):self.process()
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),0)
    def test_duplicate_and_concurrent_processing(self):
        results=self.h.race([lambda:self.process()]*4)
        self.assertEqual(results.count('credited'),1);self.assertEqual(results.count('duplicate'),3)
        self.assertEqual(self.process(),'duplicate');self.assertEqual(rdb.get_wallet_balance_cents(self.rid),1200);self.clean()
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM reseller_security_events WHERE reseller_id=? AND event_type='reseller_bep20_accounted'",self.rid),1)
    def test_wallet_failure_rolls_back_every_financial_record(self):
        with patch.object(rdb,'post_wallet_transaction',side_effect=RuntimeError('test ledger unavailable')):
            with self.assertRaises(RuntimeError):self.process()
        for table in ('reseller_bep20_events','reseller_bep20_claims'):
            self.assertEqual(self.scalar('SELECT COUNT(*) FROM '+table+' WHERE '+('address' if table.endswith('events') else 'tx_hash')+'=?',self.dest if table.endswith('events') else self.h.tx('bep20')),0)
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),0)
        self.assertEqual(self.process(),'credited');self.clean()
    def test_audit_failure_rolls_back_ledger_carry_and_claim(self):
        with patch.object(rdb,'log_security_event',side_effect=RuntimeError('test audit unavailable')):
            with self.assertRaises(RuntimeError):self.process(10**18+1)
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM reseller_bep20_carry WHERE reseller_id=?',self.rid),0)
        self.assertEqual(self.process(10**18+1),'credited');self.clean()
    def test_legacy_customer_then_new_event_conflict(self):
        uid,cid,expected,memo=self.h.customer('bep20')
        self.assertTrue(legacy.settle_customer(cid,'bep20',self.h.tx('bep20'),expected)['posted'])
        self.assertEqual(self.process(),'review');self.assertEqual(rdb.get_wallet_balance_cents(self.rid),0)
    def test_new_then_customer_or_legacy_reseller_conflict(self):
        self.process()
        uid,cid,expected,memo=self.h.customer('bep20')
        self.assertFalse(legacy.settle_customer(cid,'bep20',self.h.tx('bep20'),expected)['posted'])
        invoice=self.h.deposit('bep20');self.assertEqual(self.h.credit(invoice)['result'],'payment_used')
        self.assertEqual(db.get_balance(uid),0);self.clean()
    def test_legacy_new_customer_race_one_winner(self):
        uid,cid,expected,memo=self.h.customer('bep20')
        results=self.h.race([lambda:self.process(),lambda:legacy.settle_customer(cid,'bep20',self.h.tx('bep20'),expected)])
        wins=sum(x=='credited' if isinstance(x,str) else bool(x['posted']) for x in results)
        self.assertEqual(wins,1);self.clean()
    def test_suspension_blocks_ui_but_incoming_payment_is_preserved(self):
        rdb.set_reseller_status(self.rid,'suspended')
        with self.assertRaises(core.DepositUnavailable):core.allocate_address(self.uid,self.cfg)
        with self.assertRaises(core.DepositUnavailable):core.account_view(self.uid,self.cfg)
        self.assertEqual(self.process(),'credited');self.clean()
    def test_unrelated_user_cannot_read_assignment(self):
        with self.assertRaises(core.DepositUnavailable):core.account_view(self.uid+99999999,self.cfg)
    def test_restart_checkpoint_and_duplicate_scan(self):
        log=self.log();rpc=RPC([log])
        self.assertEqual(core.scan_once(self.cfg,rpc,self.provider),'advanced')
        self.assertEqual(self.scalar('SELECT next_block FROM reseller_bep20_scan WHERE scan_key=?',self.cfg.key),100)
        db.close_thread_connection()
        self.assertEqual(core.scan_once(self.cfg,rpc,self.provider),'advanced')
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),1200);self.clean()
    def test_pending_custody_does_not_advance_checkpoint(self):
        log=self.log();rpc=RPC([log]);self.provider.status='pending'
        self.assertEqual(core.scan_once(self.cfg,rpc,self.provider),'pending_custody')
        self.assertEqual(self.scalar('SELECT next_block FROM reseller_bep20_scan WHERE scan_key=?',self.cfg.key),90)
        self.provider.status='accepted';self.assertEqual(core.scan_once(self.cfg,rpc,self.provider),'advanced');self.clean()
    def test_reorg_checkpoint_pauses_without_duplicate_credit(self):
        rpc=RPC([self.log()]);core.scan_once(self.cfg,rpc,self.provider)
        rpc.blocks[99]='0x'+'f'*64
        with self.assertRaises(core.DepositUnavailable):core.scan_once(self.cfg,rpc,self.provider)
        self.assertEqual(self.scalar('SELECT paused FROM reseller_bep20_scan WHERE scan_key=?',self.cfg.key),1)
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),1200)
    def test_crash_mid_scan_resume_is_idempotent(self):
        logs=[self.log(index=0),self.log(index=1)];rpc=RPC(logs)
        original=self.provider.acceptance
        def crash(cfg,event,ref):
            if event.log_index==1:raise TimeoutError('test outage')
            return original(cfg,event,ref)
        with patch.object(self.provider,'acceptance',side_effect=crash):
            with self.assertRaises(core.DepositUnavailable):core.scan_once(self.cfg,rpc,self.provider)
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),1200)
        self.assertEqual(core.scan_once(self.cfg,rpc,self.provider),'advanced')
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),2400);self.clean()
    def test_invalid_asset_startup_rejected(self):
        rpc=RPC([]);core.validate_rpc(self.cfg,rpc)
        with patch.object(rpc,'call',return_value=(6).to_bytes(32,'big')):
            with self.assertRaises(core.DepositUnavailable):core.validate_rpc(self.cfg,rpc)
    def test_worker_http_outside_transaction(self):
        rpc=RPC([self.log()]);original=self.provider.acceptance
        def check(*args):
            self.assertFalse(db.get_conn().in_transaction)
            return original(*args)
        with patch.object(self.provider,'acceptance',side_effect=check):
            self.assertEqual(worker.cycle(self.cfg,self.provider,rpc),'advanced')
    def test_feature_disabled_and_no_fake_adapter(self):
        with patch.dict(os.environ,{'RESELLER_BEP20_UNIQUE_ENABLED':''}):
            self.assertFalse(core.requested());self.assertEqual(worker.main(['--once']),0)
            with self.assertRaises(core.DepositUnavailable):core.runtime()
        with self.assertRaises(core.DepositUnavailable):core.production_adapter(self.cfg)
    def test_ui_exact_fraction_and_length(self):
        self.process(6*10**15)
        view=core.account_view(self.uid,self.cfg)
        text,markup=ui.build_reseller_permanent_bep20(view,self.cfg)
        self.assertIn('0.006 USDT',text);self.assertIn('fraction retained',text)
        self.assertNotIn('Wallet credited',text);self.assertLess(len(text),4096)
        self.assertIn(self.dest,text)
    def test_requested_unqualified_ui_never_displays_receiving_address(self):
        q=SimpleNamespace(message=SimpleNamespace(reply_photo=AsyncMock()))
        with patch.dict(os.environ,{'RESELLER_BEP20_UNIQUE_ENABLED':'1'}),patch.object(ui,'edit_or_send',new=AsyncMock()) as send:
            asyncio.run(ui._handle_reseller_topup_callback(q,self.uid,'res_topup_method_bep20'))
        text=send.call_args.args[1]
        self.assertIn('unavailable',text);self.assertNotIn(self.dest,text);q.message.reply_photo.assert_not_awaited()
    def test_reorg_unconfirmed_never_credits_then_canonical_retry(self):
        log=self.log();rpc=RPC([log]);rpc.block_number=96
        with self.assertRaises(core.DepositUnavailable):core.process_log(self.cfg,rpc,self.provider,log)
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),0)
        rpc.block_number=200
        self.assertEqual(core.process_log(self.cfg,rpc,self.provider,log),'credited');self.clean()
    def test_reorg_during_custody_verification_pauses_range(self):
        log=self.log();rpc=RPC([log]);original=self.provider.acceptance
        def change(*args):
            rpc.blocks[99]='0x'+'f'*64
            return original(*args)
        with patch.object(self.provider,'acceptance',side_effect=change):
            with self.assertRaises(core.DepositUnavailable):core.scan_once(self.cfg,rpc,self.provider)
        self.assertEqual(self.scalar('SELECT paused FROM reseller_bep20_scan WHERE scan_key=?',self.cfg.key),1)
        with self.assertRaises(core.DepositUnavailable):self.process(index=2)
    def test_concurrent_scanner_exactly_once(self):
        log=self.log();rpc=RPC([log])
        self.h.race([lambda:core.scan_once(self.cfg,rpc,self.provider)]*4)
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),1200);self.clean()
    def test_empty_pool_is_pending_not_legacy_fallback(self):
        uid=self.uid+50000000;db.ensure_user_id(uid);rs=rdb.create_reseller('No pool',uid,'none');rdb.set_reseller_status(rs['id'],'active')
        with self.assertRaisesRegex(core.DepositUnavailable,'provisioning pending'):
            core.allocate_address(uid,replace(self.cfg,wallet_ref='unfilled-wallet'))
    def test_no_custody_or_rpc_inside_atomic(self):
        rpc=RPC([self.log()]);original=rpc.get_transaction_receipt
        def check(*args):
            self.assertFalse(db.get_conn().in_transaction)
            return original(*args)
        with patch.object(rpc,'get_transaction_receipt',side_effect=check):
            core.scan_once(self.cfg,rpc,self.provider)
    def test_changed_inventory_never_reassigns_permanent_address(self):
        cfg=replace(self.cfg,provider='other-provider')
        with self.assertRaises(core.DepositUnavailable):core.import_inventory(cfg,self.provider)
        self.assertEqual(core.allocate_address(self.uid,self.cfg)['address'],self.dest)
    def test_settings_strict_and_no_automatic_token_fallback(self):
        with patch.dict(os.environ,{'RESELLER_BEP20_TOKEN':'0x'+'b'*40}):
            with self.assertRaises(core.DepositUnavailable):core.load_settings()
        with self.assertRaises(core.DepositUnavailable):replace(self.cfg,minimum_units=0)
        with self.assertRaises(core.DepositUnavailable):replace(self.cfg,chain_id=1)
        with self.assertRaises(core.DepositUnavailable):replace(self.cfg,confirmations=0)
    def test_many_subcent_deposits_conserve_units(self):
        for i in range(17):self.process(10**15+1,index=i)
        totals=core.reconcile_exact(self.rid,self.cfg)
        self.assertEqual(int(totals['raw_units']),17*(10**15+1))
        self.assertEqual(totals['credited_cents'],1)
        self.assertEqual(totals['remainder_units'],str(7*10**15+17));self.clean()
    def test_changed_observation_identity_is_quarantined(self):
        self.provider.status='pending';self.process(10**18)
        self.provider.status='accepted'
        self.assertEqual(self.process(2*10**18),'review')
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),0)
    def test_reconciliation_detects_tampered_carry_sequence(self):
        self.process(6*10**15);self.process(7*10**15,index=1)
        self.assertEqual(core.reconcile_exact(self.rid,self.cfg)['problems'],[])
        with fixtures.pgcompat.atomic(db.get_conn()):
            db.get_cursor().execute('UPDATE reseller_bep20_events SET account_seq=99 WHERE address=? AND log_index=1',(self.dest,))
        self.assertIn('carry_chain',core.reconcile_exact(self.rid,self.cfg)['problems'])
    def test_schema_migration_is_idempotent_preserves_old_invoices(self):
        invoice=self.h.deposit('binance')
        from payments.unique_bep20_schema import ensure_schema
        with fixtures.pgcompat.atomic(db.get_conn()):
            ensure_schema(db.get_cursor());ensure_schema(db.get_cursor())
        self.assertEqual(self.h.credit(invoice)['result'],'posted')
        self.assertEqual(core.allocate_address(self.uid,self.cfg)['address'],self.dest)
    def test_log_unknown_owner_rejected(self):
        log=self.log(dest='0x'+'e'*40)
        with self.assertRaises(core.DepositUnavailable):core.process_log(self.cfg,RPC([log]),self.provider,log)
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),0)
    def test_bounded_sanitized_worker_dependency_failure(self):
        import logging
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ,{'RESELLER_BEP20_UNIQUE_ENABLED':'1'}))
            stack.enter_context(patch.object(core,'runtime',return_value=(self.cfg,self.provider)))
            stack.enter_context(patch.object(worker,'cycle',side_effect=TimeoutError('private-secret-value')))
            with self.assertLogs(worker.log,level=logging.WARNING) as captured:
                self.assertEqual(worker.main(['--once']),1)
            self.assertNotIn('private-secret-value',' '.join(captured.output))
    def test_reconciliation_uses_atomic_financial_snapshot(self):
        self.process(10**18+1)
        original=core._reconcile_exact_locked
        def check(*args):
            self.assertTrue(db.get_conn().in_transaction)
            return original(*args)
        with patch.object(core,'_reconcile_exact_locked',side_effect=check):self.clean()
    def test_precision_validation_and_maximum_amount(self):
        for value in (True,1.2,'01','-1',str(2**256)):
            with self.subTest(value=value):
                with self.assertRaises(core.DepositUnavailable):core.units(value)
        self.assertEqual(core.exact_text(12345678901234567890),'12.34567890123456789')


if __name__=='__main__':unittest.main()
