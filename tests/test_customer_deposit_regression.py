"""Customer/reseller payment isolation. External providers only are simulated.

No production credentials, payments, balances, or databases are used. SQLite
settlement is real; PostgreSQL integration remains a separate deployment check.
"""
import asyncio
import contextlib
from decimal import Decimal
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

os.environ['DATABASE_URL'] = ''
os.environ['BOT_TOKEN'] = ''
import tests.test_phase17_reseller_deposits as fixtures
from payments import bep20_checker as bep, binance_checker as bn, trc20_checker as trc
from payments import reseller_deposits as reseller, deposit_settlement as shared
from database import db, reseller_db as rdb


class CustomerDepositRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.ResellerDepositTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        fixtures.ResellerDepositTests.tearDownClass()

    def setUp(self):
        self.helper = fixtures.ResellerDepositTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.context = self.helper.context

    def scalar(self, sql, *args):
        return self.helper.scalar(sql, *args)

    def customer(self, method):
        return self.helper.customer(method)

    def bep_feed(self, expected, tx=None, entries=None):
        address = reseller.destination('bep20')
        entry = {'address': bep.USDT_BEP20_CONTRACT,
                 'topics': [bep.TRANSFER_TOPIC, '0x'+'0'*64, bep._topic_address(address)],
                 'data': hex(int(Decimal(str(expected))*10**bep.USDT_DECIMALS)),
                 'blockNumber': 95, 'blockHash': '0x'+f'{95:064x}', 'logIndex': 0, 'transactionHash': tx or self.helper.tx('bep20')}
        rpc = SimpleNamespace(eth=SimpleNamespace(block_number=100,
                      get_logs=lambda query: entries if entries is not None else [entry],
                      get_transaction_receipt=lambda tx: {'status':1,'transactionHash':entry['transactionHash'],'blockNumber':95,'blockHash':entry['blockHash'],'logs':[entry]},
                      get_block=lambda height:{'hash':'0x'+f'{height:064x}'}))
        stack = contextlib.ExitStack()
        for target, kwargs in [('_config_ready', {'return_value':True}),
            ('USDT_BEP20_ADDRESS', {'new':address}), ('_get_web3', {'return_value':rpc}),
            ('_wallet_usdt_balance', {'return_value':Decimal(str(expected))}),
            ('_load_last_scanned_block', {'return_value':90})]:
            stack.enter_context(patch.object(bep,target,**kwargs))
        self.context.bot_data['last_wallet_balance'] = Decimal('0')
        return stack, entry

    def binance_feed(self, memo, amount='10', tx=None, currency='USDT'):
        tx = {'transactionId': tx or self.helper.tx('binance'), 'amount': amount,
              'currency': currency, 'note':memo}
        stack=contextlib.ExitStack()
        stack.enter_context(patch.object(bn,'_config_ready',return_value=True))
        stack.enter_context(patch.object(bn,'_recent_pay_transactions',new=AsyncMock(return_value=[tx])))
        return stack

    def assert_customer_only(self, uid, cid, balance=10):
        self.assertEqual(db.get_balance(uid), balance)
        self.assertEqual(self.scalar('SELECT status FROM deposits WHERE id=?',cid),'paid')
        self.assertEqual(rdb.get_wallet_balance_cents(self.helper.rid),0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM reseller_wallet_transactions WHERE reseller_id=?',self.helper.rid),0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM processed_txs WHERE deposit_id=? AND user_id=?',cid,uid),1)

    def test_bep_customer_checker_credits_correct_wallet_once(self):
        uid,cid,amount,_=self.customer('bep20');stack,_=self.bep_feed(amount)
        with stack:
            asyncio.run(bep.check_payments(self.context));asyncio.run(bep.check_payments(self.context))
        self.assert_customer_only(uid,cid)

    def test_binance_customer_checker_credits_correct_wallet_once(self):
        uid,cid,amount,memo=self.customer('binance')
        with self.binance_feed(memo,amount='13'):
            asyncio.run(bn.check_binance_payments(self.context));asyncio.run(bn.check_binance_payments(self.context))
        self.assert_customer_only(uid,cid)

    def test_reseller_audit_failure_cannot_stop_customer_bep_checker(self):
        uid,cid,amount,_=self.customer('bep20');stack,_=self.bep_feed(amount)
        with stack, patch.object(reseller,'post_credit_actions',new=AsyncMock(side_effect=RuntimeError('test-only-secret'))):
            asyncio.run(bep.check_payments(self.context))
        self.assert_customer_only(uid,cid)

    def test_reseller_audit_failure_cannot_stop_customer_binance_checker(self):
        uid,cid,amount,memo=self.customer('binance')
        with self.binance_feed(memo),patch.object(reseller,'post_credit_actions',new=AsyncMock(side_effect=RuntimeError('test-only-secret'))):
            asyncio.run(bn.check_binance_payments(self.context))
        self.assert_customer_only(uid,cid)

    def test_reseller_sweep_failure_cannot_stop_customer_bep_settlement(self):
        uid,cid,amount,_=self.customer('bep20');stack,_=self.bep_feed(amount)
        with stack,patch.object(reseller,'process_observations',new=AsyncMock(side_effect=RuntimeError('test-only-secret'))):
            asyncio.run(bep.check_payments(self.context))
        self.assert_customer_only(uid,cid)
        self.assertNotIn('last_scanned_block',self.context.bot_data)

    def test_reseller_sweep_failure_cannot_stop_customer_binance_settlement(self):
        uid,cid,amount,memo=self.customer('binance')
        with self.binance_feed(memo),patch.object(reseller,'process_observations',new=AsyncMock(side_effect=RuntimeError('test-only-secret'))):
            asyncio.run(bn.check_binance_payments(self.context))
        self.assert_customer_only(uid,cid)

    def test_reseller_sweep_failure_cannot_stop_customer_trc_settlement(self):
        uid,cid,amount,_=self.customer('trc20')
        with patch.object(trc,'_config_ready',return_value=True),patch.object(trc,'_recent_incoming_transfers',new=AsyncMock(return_value=[(self.helper.tx(),Decimal(str(amount)))])),patch.object(reseller,'process_observations',new=AsyncMock(side_effect=RuntimeError('test-only-secret'))):
            asyncio.run(trc.check_trc20_payments(self.context))
        self.assert_customer_only(uid,cid)

    def test_malformed_bep_event_does_not_discard_valid_payment(self):
        uid,cid,amount,_=self.customer('bep20');stack,good=self.bep_feed(amount)
        stack.close()
        bad=dict(good,data='not-hex',transactionHash='0x'+'a'*64)
        stack,_=self.bep_feed(amount,entries=[bad,good])
        with stack:asyncio.run(bep.check_payments(self.context))
        self.assert_customer_only(uid,cid)

    def test_invalid_binance_asset_or_wrong_note_never_credits(self):
        for kind in ['asset','note','underpayment']:
            uid,cid,amount,memo=self.customer('binance')
            with self.binance_feed('WRONGNOTE' if kind=='note' else memo,
                amount='9' if kind=='underpayment' else '10',currency='PEPE' if kind=='asset' else 'USDT'):
                asyncio.run(bn.check_binance_payments(self.context))
            self.assertEqual(db.get_balance(uid),0)
            self.assertEqual(self.scalar('SELECT status FROM deposits WHERE id=?',cid),'pending')

    def test_wrong_bep_contract_destination_and_unconfirmed_are_rejected(self):
        uid,cid,amount,_=self.customer('bep20');stack,good=self.bep_feed(amount);stack.close()
        entries=[dict(good,address='0x'+'a'*40),dict(good,blockNumber=99),dict(good,topics=[bep.TRANSFER_TOPIC,'0x'+'0'*64,bep._topic_address('0x'+'2'*40)])]
        stack,_=self.bep_feed(amount,entries=entries)
        # Prevent the existing legacy balance-delta path from hiding bad evidence.
        self.helper.deposit('bep20')
        with stack:asyncio.run(bep.check_payments(self.context))
        self.assertEqual(db.get_balance(uid),0)
        self.assertEqual(self.scalar('SELECT status FROM deposits WHERE id=?',cid),'pending')

    def test_customer_commit_failure_rolls_back_and_retry_recovers(self):
        uid,cid,amount,_=self.customer('bep20');tx=self.helper.tx('bep20')
        # Trigger failure AFTER balance UPDATE, before final status can commit.
        db.cur.execute("CREATE TEMP TRIGGER fail_deposit_status BEFORE UPDATE OF status ON deposits WHEN NEW.id="+str(cid)+" AND NEW.status='paid' BEGIN SELECT RAISE(ABORT,'synthetic status failure'); END")
        db.conn.commit()
        result=shared.settle_customer_safely(cid,'bep20',tx,amount)
        self.assertTrue(result['deferred']);self.assertEqual(db.get_balance(uid),0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM processed_txs WHERE tx_hash=?',tx),0)
        self.assertEqual(self.scalar('SELECT status FROM deposits WHERE id=?',cid),'pending')
        db.cur.execute('DROP TRIGGER fail_deposit_status');db.conn.commit()
        self.assertTrue(shared.settle_customer(cid,'bep20',tx,amount)['posted']);self.assert_customer_only(uid,cid)

    def test_customer_concurrent_processing_exactly_once(self):
        uid,cid,amount,memo=self.customer('binance');tx=self.helper.tx('binance')
        outcomes=self.helper.race([lambda:shared.settle_customer(cid,'binance',tx,10,notes=memo)]*4)
        self.assertEqual(sum(x['posted'] for x in outcomes),1);self.assert_customer_only(uid,cid)

    def test_reseller_checker_never_credits_customer(self):
        uid,cid,amount,memo=self.customer('binance');d=self.helper.deposit('binance')
        with self.binance_feed(d['memo']):asyncio.run(bn.check_binance_payments(self.context))
        self.assertEqual(db.get_balance(uid),0);self.assertEqual(self.scalar('SELECT status FROM deposits WHERE id=?',cid),'pending')
        self.assertEqual(reseller.row(d['id'])['status'],'paid');self.assertEqual(rdb.get_wallet_balance_cents(self.helper.rid),1000)
        self.helper.assert_clean()

    def test_customer_and_reseller_cross_table_race_has_one_winner(self):
        d=self.helper.deposit('bep20');uid,cid,amount,memo=self.helper.customer('bep20');tx=self.helper.tx('bep20')
        results=self.helper.race([lambda:shared.settle_customer(cid,'bep20',tx,amount),lambda:self.helper.credit(d,tx=tx)])
        self.assertEqual(sum(x.get('posted',x.get('result')=='posted') for x in results),1)
        self.assertEqual(round(db.get_balance(uid)*100)+rdb.get_wallet_balance_cents(self.helper.rid),1000)

    def test_missing_ownership_lock_fails_closed_without_partial_credit(self):
        uid,cid,amount,_=self.customer('bep20')
        db.cur.execute("DELETE FROM settings WHERE key='payment.namespace.lock'");db.conn.commit()
        try:
            result=shared.settle_customer_safely(cid,'bep20',self.helper.tx('bep20'),amount)
            self.assertTrue(result['deferred']);self.assertEqual(db.get_balance(uid),0)
            self.assertEqual(self.scalar('SELECT status FROM deposits WHERE id=?',cid),'pending')
        finally:
            db.cur.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('payment.namespace.lock','1')");db.conn.commit()

    def test_expired_customer_payment_is_not_automatically_credited(self):
        uid,cid,amount,memo=self.customer('binance')
        db.cur.execute("UPDATE deposits SET created='2000-01-01 00:00:00' WHERE id=?",(cid,));db.conn.commit()
        self.assertFalse(shared.settle_customer(cid,'binance',self.helper.tx('binance'),10,notes=memo)['posted'])
        self.assertEqual(db.get_balance(uid),0)

    def test_legacy_cross_workflow_amount_collision_is_held_not_misrouted(self):
        d=self.helper.deposit('bep20');uid,cid,amount,_=self.customer('bep20')
        db.cur.execute('UPDATE deposits SET unique_amount=? WHERE id=?',(d['expected_amount'],cid));db.conn.commit()
        stack,_=self.bep_feed(d['expected_amount'])
        with stack:asyncio.run(bep.check_payments(self.context))
        self.assertEqual(db.get_balance(uid),0)
        self.assertEqual(rdb.get_wallet_balance_cents(self.helper.rid),0)
        self.assertEqual(reseller.row(d['id'])['status'],'pending')
        self.assertEqual(self.scalar('SELECT status FROM deposits WHERE id=?',cid),'pending')
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM processed_txs WHERE tx_hash=?',self.helper.tx('bep20')),0)
        self.assertNotIn('last_scanned_block',self.context.bot_data)

    def test_legacy_cross_workflow_memo_collision_is_held_not_misrouted(self):
        d=self.helper.deposit('binance');uid,cid,amount,_=self.customer('binance')
        db.cur.execute('UPDATE deposits SET note_code=? WHERE id=?',(d['memo'],cid));db.conn.commit()
        with self.binance_feed(d['memo']):asyncio.run(bn.check_binance_payments(self.context))
        self.assertEqual(db.get_balance(uid),0)
        self.assertEqual(rdb.get_wallet_balance_cents(self.helper.rid),0)
        self.assertEqual(reseller.row(d['id'])['status'],'pending')
        self.assertEqual(self.scalar('SELECT status FROM deposits WHERE id=?',cid),'pending')

    def checkout_product(self):
        db.cur.execute("INSERT INTO products(name,price,stock,delivery,active) VALUES('Deposit isolation product',10,1,'code_stock',1)")
        pid=db.cur.lastrowid
        db.cur.execute('INSERT INTO product_stock(product_id,delivery,sold) VALUES(?,?,0)',(pid,'TEST-CHECKOUT-CODE'))
        db.conn.commit()
        return pid

    def test_direct_checkout_amount_does_not_collide_with_reseller_invoice(self):
        from products import product_handlers as products
        # Force the old five-decimal checkout suffix to collide with a current
        # reseller invoice. A shared allocator must reject it and choose again.
        with patch.object(shared.random,'randint',return_value=1000):d=self.helper.deposit('bep20')
        uid=self.helper.uid+3000000;db.ensure_user_id(uid);pid=self.checkout_product()
        def suffix(low,high):
            # New shared checkout allocator first collides, then chooses .00101.
            suffix.calls+=1
            return 100 if suffix.calls==1 else 101
        suffix.calls=0
        with patch.object(shared.random,'randint',side_effect=suffix),patch.object(products,'edit_or_send',new=AsyncMock()):
            asyncio.run(products.start_direct_checkout(SimpleNamespace(),uid,pid,method='bep20'))
        amount=self.scalar('SELECT unique_amount FROM deposits WHERE user_id=? AND status=\'pending\'',uid)
        self.assertGreater(abs(Decimal(str(amount))-Decimal(str(d['expected_amount']))),2*shared.TOL)
        self.assertEqual(round(amount,5),amount)

    def test_direct_checkout_memo_does_not_collide_with_reseller_invoice(self):
        from products import product_handlers as products
        with patch.object(shared.secrets,'choice',return_value='A'):d=self.helper.deposit('binance')
        uid=self.helper.uid+3000000;db.ensure_user_id(uid);pid=self.checkout_product()
        with patch.object(shared.secrets,'choice',side_effect=list('AAAAAAAA'+'BBBBBBBB')),patch.object(products.random,'choice',return_value='A'),patch.object(products,'edit_or_send',new=AsyncMock()):
            asyncio.run(products.start_direct_checkout(SimpleNamespace(),uid,pid,method='binance'))
        memo=self.scalar("SELECT note_code FROM deposits WHERE user_id=? AND status='pending'",uid)
        self.assertNotEqual(memo,d['memo'])

    def test_reseller_linked_telegram_user_can_top_up_customer_wallet_independently(self):
        uid=self.helper.uid
        cid,amount,memo=shared.customer_invoice(uid,10,'binance')
        with self.binance_feed(memo):asyncio.run(bn.check_binance_payments(self.context))
        self.assert_customer_only(uid,cid)

    def test_reseller_notification_timeout_is_bounded_and_sanitized(self):
        import time
        async def stalled(context):await asyncio.Event().wait()
        start=time.monotonic()
        with patch.object(reseller,'post_credit_actions',side_effect=stalled),patch.object(reseller,'POST_CREDIT_BUDGET_SECONDS',.01),self.assertLogs(reseller.__name__,level='WARNING') as logs:
            asyncio.run(reseller.post_credit_actions_safely(self.context))
        self.assertLess(time.monotonic()-start,1)
        self.assertIn('TimeoutError',str(logs.output))

    def test_reseller_optional_errors_are_logged_without_secrets(self):
        class ProviderFailure(RuntimeError):sqlstate='08006'
        failure=ProviderFailure('test-only-private-provider-token')
        with patch.object(reseller,'has_receiving_records',side_effect=failure),self.assertLogs(reseller.__name__,level='WARNING') as logs:
            self.assertTrue(reseller.has_receiving_records_safely('binance'))
        text=str(logs.output);self.assertIn('08006',text);self.assertNotIn('test-only-private',text)

    def test_failed_checkout_allocation_keeps_previous_customer_invoice(self):
        uid,cid,amount,memo=self.customer('binance');pid=self.checkout_product()
        with patch.object(shared,'allocate',side_effect=ValueError('synthetic allocation exhaustion')):
            with self.assertRaises(ValueError):shared.customer_checkout_invoice(uid,10,'binance',pid,1)
        self.assertEqual(self.scalar('SELECT status FROM deposits WHERE id=?',cid),'pending')
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM deposits WHERE user_id=?',uid),1)

    def test_customer_direct_checkouts_deliver_without_touching_reseller_ledger(self):
        from products import product_handlers as products
        from utils.crypto import decrypt_text
        uid=self.helper.uid+3000000;db.ensure_user_id(uid)
        for method in ('bep20','binance'):
            pid=self.checkout_product()
            with patch.object(products,'edit_or_send',new=AsyncMock()):
                asyncio.run(products.start_direct_checkout(SimpleNamespace(),uid,pid,method=method))
            db.cur.execute("SELECT id,unique_amount,note_code FROM deposits WHERE user_id=? AND status='pending'",(uid,));cid,amount,memo=db.cur.fetchone()
            if method=='bep20':
                stack,_=self.bep_feed(amount)
                with stack:asyncio.run(bep.check_payments(self.context))
            else:
                with self.binance_feed(memo):asyncio.run(bn.check_binance_payments(self.context))
            self.assertEqual(self.scalar('SELECT status FROM deposits WHERE id=?',cid),'paid')
            self.assertEqual(self.scalar('SELECT sold FROM product_stock WHERE product_id=?',pid),1)
            self.assertEqual(decrypt_text(self.scalar('SELECT delivery FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 1',uid)),'TEST-CHECKOUT-CODE')
        self.assertEqual(db.get_balance(uid),0)
        self.assertEqual(rdb.get_wallet_balance_cents(self.helper.rid),0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM orders WHERE user_id=?',uid),2)

    def test_binance_request_uses_existing_signed_history_contract(self):
        import hashlib,hmac
        from urllib.parse import urlsplit,parse_qsl
        response=SimpleNamespace(status_code=200,json=lambda:{'success':True,'data':[]})
        http=AsyncMock();http.get.return_value=response;http.__aenter__.return_value=http
        with patch.object(bn,'BINANCE_API_KEY','test-only-read-key'),patch.object(bn,'BINANCE_API_SECRET','test-only-signing-secret'),patch.object(bn,'_make_async_client',return_value=http):
            self.assertEqual(asyncio.run(bn._recent_pay_transactions()),[])
        call=http.get.call_args;url=urlsplit(call.args[0]);query,signature=url.query.rsplit('&signature=',1)
        self.assertEqual(url.path,'/sapi/v1/pay/transactions')
        self.assertEqual(signature,hmac.new(b'test-only-signing-secret',query.encode(),hashlib.sha256).hexdigest())
        self.assertEqual(call.kwargs['headers']['X-MBX-APIKEY'],'test-only-read-key')
        self.assertEqual(dict(parse_qsl(query))['limit'],'100')

    def test_provider_error_leaves_payment_retryable(self):
        uid,cid,amount,memo=self.customer('binance')
        with patch.object(bn,'_config_ready',return_value=True),patch.object(bn,'_recent_pay_transactions',new=AsyncMock(side_effect=TimeoutError('test-only-provider-secret'))):
            asyncio.run(bn.check_binance_payments(self.context))
        self.assertEqual(db.get_balance(uid),0);self.assertEqual(self.scalar('SELECT status FROM deposits WHERE id=?',cid),'pending')
        with self.binance_feed(memo):asyncio.run(bn.check_binance_payments(self.context))
        self.assert_customer_only(uid,cid)

if __name__=='__main__':unittest.main()
