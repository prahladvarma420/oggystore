"""Whole-project batch1: customer money and real support/WebAdmin services.
Temporary SQLite, real route/service logic, Telegram send mocked only.
"""
import math,threading,unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import tests.test_phase16_admin_fail_order as fixture
from database import db

class WholeProjectFinancialSupportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import web_admin.app as wa
        cls.wa=wa;wa.app.config['TESTING']=True;wa.DB_PATH=fixture._db_path
    def setUp(self):
        self.uid=8_850_000+fixture._n();db.ensure_user_id(self.uid)
        db.cur.execute('UPDATE users SET balance=25,referral_pending=100 WHERE user_id=?',(self.uid,));db.conn.commit()
        self.client=fixture.Phase16AdminFailOrderTests()._owner()
    def scalar(self,sql,params=()):db.cur.execute(sql,params);return db.cur.fetchone()[0]
    def test_customer_balance_nonfinite_rejected_before_mutation(self):
        for amount in ('NaN','Infinity','-Infinity',True,[],{}):
            response=self.client.post(f'/api/users/{self.uid}/balance',json={'amount':amount},headers={'X-CSRF-Token':'test-csrf'})
            self.assertEqual(response.status_code,400,str(amount));self.assertEqual(db.get_balance(self.uid),25)
    def test_balance_service_rejects_nan_and_infinity(self):
        for amount in (float('nan'),float('inf'),-float('inf')):
            with self.assertRaises(ValueError):db.add_balance(self.uid,amount)
            self.assertEqual(db.get_balance(self.uid),25)
    def test_finite_credit_cannot_overflow_existing_customer_balance(self):
        db.cur.execute('UPDATE users SET balance=1e308 WHERE user_id=?',(self.uid,));db.conn.commit()
        with self.assertRaises(ValueError):db.add_balance(self.uid,1e308)
        self.assertEqual(db.get_balance(self.uid),1e308)
    def test_balance_valid_positive_negative_and_zero_preserved(self):
        for amount in (5,-3,0):db.add_balance(self.uid,amount)
        self.assertEqual(db.get_balance(self.uid),27)
    def test_withdrawal_nonfinite_does_not_mutate_or_insert(self):
        for amount in (float('nan'),float('inf'),-float('inf')):
            self.assertIsNone(db.create_withdrawal_request(self.uid,amount,'test-wallet'))
            self.assertEqual(self.scalar('SELECT referral_pending FROM users WHERE user_id=?',(self.uid,)),100)
            self.assertEqual(self.scalar('SELECT COUNT(*) FROM withdrawal_requests WHERE user_id=?',(self.uid,)),0)
    def concurrent(self,callback,prefix):
        barrier=threading.Barrier(2)
        def work():
            actual=db.get_cursor()
            class Tap:
                def execute(self,sql,*args):self.sql=sql;return actual.execute(sql,*args)
                def fetchone(self):
                    value=actual.fetchone()
                    if self.sql.startswith(prefix) and not db.get_conn().in_transaction:barrier.wait(timeout=5)
                    return value
                def __getattr__(self,name):return getattr(actual,name)
            db._tls.cur=Tap()
            try:return callback()
            finally:db.close_thread_connection()
        with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(lambda _:work(),range(2)))
    def test_concurrent_withdrawal_cannot_overdraw_pending_referral(self):
        results=self.concurrent(lambda:db.create_withdrawal_request(self.uid,80,'test-wallet'),'SELECT COALESCE(referral_pending')
        self.assertEqual(sum(x is not None for x in results),1)
        self.assertEqual(self.scalar('SELECT referral_pending FROM users WHERE user_id=?',(self.uid,)),20)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM withdrawal_requests WHERE user_id=?',(self.uid,)),1)
    def test_concurrent_rejection_refunds_pending_exactly_once(self):
        wid=db.create_withdrawal_request(self.uid,80,'test-wallet')
        self.concurrent(lambda:db.update_withdrawal_status(wid,'rejected'),'SELECT user_id, amount, status')
        self.assertEqual(self.scalar('SELECT referral_pending FROM users WHERE user_id=?',(self.uid,)),100)
        self.assertEqual(self.scalar('SELECT status FROM withdrawal_requests WHERE id=?',(wid,)),'rejected')
    def test_withdrawal_insert_failure_rolls_back_pending_amount(self):
        db.cur.execute("CREATE TEMP TRIGGER audit_abort_withdraw BEFORE INSERT ON withdrawal_requests BEGIN SELECT RAISE(ABORT,'test-only injected failure'); END");db.conn.commit()
        try:
            with self.assertRaises(Exception):db.create_withdrawal_request(self.uid,80,'test-wallet')
            self.assertEqual(self.scalar('SELECT referral_pending FROM users WHERE user_id=?',(self.uid,)),100)
        finally:db.get_conn().rollback();db.cur.execute('DROP TRIGGER audit_abort_withdraw');db.conn.commit()
    def test_repeated_web_withdrawal_action_no_false_notification(self):
        wid=db.create_withdrawal_request(self.uid,20,'test-wallet')
        with patch.object(self.wa,'tg_send',return_value=(True,'test-only')) as send:
            for status,expected in [('approved',200),('rejected',409)]:
                result=self.client.post(f'/api/withdrawals/{wid}/action',json={'status':status},headers={'X-CSRF-Token':'test-csrf'})
                self.assertEqual(result.status_code,expected)
            self.assertEqual(send.call_count,1)
        self.assertEqual(self.scalar('SELECT status FROM withdrawal_requests WHERE id=?',(wid,)),'approved')
    def test_registered_api_get_routes_reject_anonymous_and_inventory_runtime(self):
        import json,re
        from reseller_api import create_app
        api=create_app({'TESTING':True,'API_ENVIRONMENT':'test','INIT_DATABASE':False})
        rows=[]
        for label,app in [('webadmin',self.wa.app),('reseller',api)]:
            client=app.test_client()
            for rule in app.url_map.iter_rules():
                methods=sorted(rule.methods-{'OPTIONS','HEAD'})
                row={'app':label,'path':rule.rule,'methods':methods,'endpoint':rule.endpoint}
                if 'GET' in methods and rule.rule.startswith('/api/'):
                    path=re.sub(r'<(?:[^:>]+:)?([^>]+)>',lambda m:'1' if '<int:'+m.group(1)+'>' in rule.rule else 'missing-test-id',rule.rule)
                    # Use independent synthetic source IPs so this auth matrix does not
                    # exhaust one anonymous budget; rate-limit suites stay intact.
                    response=client.get(path,environ_base={'REMOTE_ADDR':f'127.0.0.{len(rows)+1}'})
                    row['anonymous_status']=response.status_code
                    self.assertEqual(response.status_code,401,(label,path,response.get_data(as_text=True)[:300]))
                rows.append(row)
        print('ROUTE_EVIDENCE '+json.dumps(rows),flush=True)
    def test_security_backup_indicator_validates_actual_key_not_presence(self):
        import os
        from cryptography.fernet import Fernet
        for key,expected in [('test-invalid-key',False),(Fernet.generate_key().decode(),True),('',False)]:
            with patch.dict(os.environ,{'BACKUP_ENCRYPTION_KEY':key,'DATA_ENCRYPTION_KEY':''}):
                response=self.client.get('/api/security')
            self.assertEqual(response.status_code,200)
            self.assertIs(response.get_json()['encrypted_backups'],expected)
    def test_support_same_id_customer_reply_close_and_foreign_denial(self):
        tid=db.create_ticket(self.uid,'<support>','opening')
        with patch.object(self.wa,'tg_send',return_value=(True,'test-only')) as send:
            response=self.client.post(f'/api/tickets/{tid}/reply',json={'reply':'admin response'},headers={'X-CSRF-Token':'test-csrf'})
            self.assertEqual(response.status_code,200)
            db.add_user_ticket_reply(tid,self.uid,'same thread followup')
            with self.assertRaises(ValueError):db.add_user_ticket_reply(tid,self.uid+1000,'foreign')
            messages,_=db.get_ticket_messages(tid,100)
            self.assertEqual([m[4] for m in messages],['opening','admin response','same thread followup'])
            for _ in range(2):self.assertEqual(self.client.post(f'/api/tickets/{tid}/close',json={},headers={'X-CSRF-Token':'test-csrf'}).status_code,200)
            self.assertEqual(send.call_count,2)
            with self.assertRaises(ValueError):db.add_user_ticket_reply(tid,self.uid,'after close')
            self.assertEqual(self.client.post(f'/api/tickets/{tid}/reply',json={'reply':'after close'},headers={'X-CSRF-Token':'test-csrf'}).status_code,409)
        self.assertEqual(db.get_ticket(tid)[6],'closed')
        messages,_=db.get_ticket_messages(tid,100);self.assertEqual(messages[-1][4],db.SUPPORT_RESOLUTION_MESSAGE)

if __name__=='__main__':unittest.main()
