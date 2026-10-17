"""Actual Binance fetch/checker boundaries with simulated HTTP, real SQLite settlement.

No live Binance keys, signed network requests or production payments are used.
HTTP 451 tests establish safe behavior, not the actual provider's eligibility decision.
"""
import os
os.environ['DATABASE_URL']=''
os.environ['BOT_TOKEN']=''
import asyncio
import contextlib
import io
import unittest
from unittest.mock import patch,AsyncMock
import httpx
import tests.test_phase17_reseller_deposits as fixtures
from payments import binance_checker as checker


class BinanceProviderFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixtures.ResellerDepositTests.setUpClass()
    @classmethod
    def tearDownClass(cls):fixtures.ResellerDepositTests.tearDownClass()
    def setUp(self):
        self.h=fixtures.ResellerDepositTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.context=self.h.context
        for attr,value in [('BINANCE_API_KEY','test-only-read-key'),('BINANCE_API_SECRET','test-only-signing-secret'),('BINANCE_PAY_ID','12345678')]:
            p=patch.object(checker,attr,value);p.start();self.addCleanup(p.stop)
    def run_fetch(self,status=200,body=None,headers=None):
        def handler(request):
            self.assertEqual(request.url.path,'/sapi/v1/pay/transactions')
            self.assertEqual(request.headers['X-MBX-APIKEY'],'test-only-read-key')
            return httpx.Response(status,json=body,headers=headers,request=request)
        with patch.object(checker,'_make_async_client',side_effect=lambda:httpx.AsyncClient(transport=httpx.MockTransport(handler))):
            return asyncio.run(checker._recent_pay_transactions())
    def test_provider_451_sanitized_and_no_retry_to_other_hosts(self):
        with self.assertRaises(checker.BinanceFetchError) as error:
            self.run_fetch(451,{'message':'test-private-body signed-url secret-material'})
        self.assertEqual(error.exception.category,'provider_restricted')
        self.assertEqual(error.exception.retry_seconds,300)
        for text in ('test-private-body','signed-url','secret-material','https://'):
            self.assertNotIn(text,str(error.exception))
    def test_status_and_numeric_error_categories(self):
        for status,body,expected in [(401,{},'authentication'),(403,{},'access_denied'),
                (429,{},'rate_limited'),(418,{},'rate_blocked'),(503,{},'provider_server'),
                (400,{'code':-2015},'key_ip_or_permission'),(400,{'code':-1021},'clock_skew'),
                (400,{'code':-1022},'invalid_signature')]:
            with self.subTest(status=status,expected=expected):
                with self.assertRaises(checker.BinanceFetchError) as error:self.run_fetch(status,body)
                self.assertEqual(error.exception.category,expected)
    def test_rate_limit_retry_after_is_bounded(self):
        with self.assertRaises(checker.BinanceFetchError) as error:self.run_fetch(429,{},headers={'Retry-After':'99999'})
        self.assertEqual(error.exception.retry_seconds,3600)
    def test_transport_failure_does_not_return_signed_url(self):
        async def fail(url,**kwargs):raise httpx.ConnectError('test-key private-signature '+url)
        client=AsyncMock();client.__aenter__.return_value=client;client.get.side_effect=fail
        with patch.object(checker,'_make_async_client',return_value=client):
            with self.assertRaises(checker.BinanceFetchError) as error:asyncio.run(checker._recent_pay_transactions())
        self.assertEqual(error.exception.category,'transport')
        self.assertNotIn('test-key',str(error.exception));self.assertNotIn('signature=',str(error.exception))
    def test_malformed_success_response_is_not_payment_history(self):
        for body in ({'success':False,'data':[]},{'success':'false','data':[]},
                     {'success':True,'data':{}},{'success':True,'data':['not-transfer']},[]):
            with self.subTest(body=body):
                with self.assertRaises(checker.BinanceFetchError):self.run_fetch(200,body)
    def test_valid_history_uses_existing_contract(self):
        rows=[{'currency':'USDT','amount':'10','note':'TESTNOTE','transactionId':'test-binance-id'}]
        self.assertEqual(self.run_fetch(200,{'success':True,'data':rows}),rows)
    def test_451_keeps_customer_pending_and_wallet_unchanged(self):
        uid,dep,amount,memo=self.h.customer('binance')
        with patch.object(checker,'_recent_pay_transactions',new=AsyncMock(side_effect=checker.BinanceFetchError('provider_restricted',451,300))),contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(checker.check_binance_payments(self.context))
        self.assertEqual(self.h.scalar('SELECT status FROM deposits WHERE id=?',dep),'pending')
        self.assertEqual(fixtures.db.get_balance(uid),0)
        self.assertEqual(fixtures.rdb.get_wallet_balance_cents(self.h.rid),0)
        self.assertEqual(self.context.bot_data['binance_verification_health']['category'],'provider_restricted')
    def test_451_keeps_reseller_invoice_pending(self):
        invoice=self.h.deposit('binance')
        with patch.object(checker,'_recent_pay_transactions',new=AsyncMock(side_effect=checker.BinanceFetchError('provider_restricted',451,300))),contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(checker.check_binance_payments(self.context))
        self.assertEqual(fixtures.service.row(invoice['id'])['status'],'pending')
        self.assertEqual(fixtures.rdb.get_wallet_balance_cents(self.h.rid),0)
    def test_cooldown_then_recovery_credits_exactly_once(self):
        uid,dep,amount,memo=self.h.customer('binance')
        rows=[{'currency':'USDT','amount':'10','note':memo,'transactionId':self.h.tx('binance')}]
        poll=AsyncMock(side_effect=[checker.BinanceFetchError('provider_restricted',451,300),rows,rows])
        with patch.object(checker,'_recent_pay_transactions',new=poll),contextlib.redirect_stdout(io.StringIO()):
            with patch.object(checker.time,'monotonic',return_value=10):asyncio.run(checker.check_binance_payments(self.context))
            with patch.object(checker.time,'monotonic',return_value=11):asyncio.run(checker.check_binance_payments(self.context))
            self.assertEqual(poll.await_count,1)
            with patch.object(checker.time,'monotonic',return_value=311):asyncio.run(checker.check_binance_payments(self.context))
            with patch.object(checker.time,'monotonic',return_value=312):asyncio.run(checker.check_binance_payments(self.context))
        self.assertEqual(fixtures.db.get_balance(uid),10)
        self.assertEqual(self.h.scalar('SELECT status FROM deposits WHERE id=?',dep),'paid')
        self.assertNotIn('binance_verification_health',self.context.bot_data)
    def test_no_raw_error_body_in_checker_logs(self):
        self.h.customer('binance')
        stdout=io.StringIO()
        failure=checker.BinanceFetchError('provider_restricted',451,300)
        with patch.object(checker,'_recent_pay_transactions',new=AsyncMock(side_effect=failure)),contextlib.redirect_stdout(stdout),self.assertLogs('oggystore_bot',level='ERROR') as logged:
            asyncio.run(checker.check_binance_payments(self.context))
        text=stdout.getvalue()+' '.join(logged.output)
        self.assertIn('provider_restricted',text)
        self.assertNotIn('test-only-read-key',text);self.assertNotIn('test-only-signing-secret',text)


if __name__=='__main__':unittest.main()
