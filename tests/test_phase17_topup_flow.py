"""17.2 thin Telegram seams and real persistent deposit services.

External provider and Telegram boundaries only are mocked. SQLite temporary
fixtures, real shared claims / ledger / security and owner action records.
"""
import asyncio,json,threading,unittest
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock
import tests.test_phase17_reseller_deposits as support
from payments import reseller_deposits as deposits,deposit_settlement as shared
from handlers import reseller_handlers as handlers
from utils.states import user_state,RES_TOPUP_AMOUNT,RES_TOPUP_PAYMENT
from utils.timezone import now_text
from PIL import Image
from pathlib import Path
import ast,hashlib

db,rdb=support.db,support.rdb
class TopupFlowTests(support.ResellerDepositTests):
    # Reuse fixture helpers, not the 17.1 cases: loader below selects this module's
    # own test methods so each suite's execution count is accurate.
    def setUp(self):
        super().setUp();self.addCleanup(lambda:user_state.pop(self.uid,None));self.addCleanup(lambda:handlers._topup_drafts.pop(self.uid,None))
    def q(self):return SimpleNamespace(message=SimpleNamespace(reply_photo=AsyncMock()),answer=AsyncMock())
    def update(self):return SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock(),reply_photo=AsyncMock()))
    def callback(self,data):
        q=self.q()
        with patch.object(handlers,'edit_or_send',new=AsyncMock()) as edit:
            self.assertTrue(asyncio.run(handlers.handle_reseller_callback(q,self.uid,data,self.context)))
            return q,edit
    def test_active_can_start_picker_and_create_via_amount_message(self):
        q,edit=self.callback('res_topup');markup=edit.await_args.args[2];self.assertEqual(len(markup.inline_keyboard),4)
        self.callback('res_topup_method_trc20');self.assertEqual(user_state[self.uid],RES_TOPUP_AMOUNT)
        update=self.update();self.assertTrue(asyncio.run(handlers.handle_reseller_message(update,self.uid,'10.00',RES_TOPUP_AMOUNT)))
        saved=deposits.session(self.uid);self.assertEqual(saved['amount'],10);self.assertEqual(user_state[self.uid],RES_TOPUP_PAYMENT)
        self.assertEqual(update.message.reply_photo.await_count,1);self.assertEqual(update.message.reply_text.await_args.kwargs['parse_mode'],'HTML')
    def test_suspended_cannot_initiate_or_manipulate(self):
        d=self.deposit();rdb.set_reseller_status(self.rid,'suspended')
        for action in ['res_topup','res_topup_method_trc20',f'res_topup_show_{d["id"]}',f'res_topup_cancel_{d["id"]}','res_topup_back']:
            q,edit=self.callback(action);self.assertIn('Active reseller',edit.await_args.args[1])
        self.assertEqual(deposits.row(d['id'])['status'],'pending')
    def test_status_revalidated_between_selection_and_creation(self):
        self.callback('res_topup_method_trc20');rdb.set_reseller_status(self.rid,'suspended');u=self.update()
        asyncio.run(handlers.handle_reseller_message(u,self.uid,'10',RES_TOPUP_AMOUNT))
        self.assertNotIn(self.uid,user_state);self.assertEqual(self.scalar('SELECT COUNT(*) FROM reseller_deposits WHERE reseller_id=?',self.rid),0)
    def test_blocked_identity_and_other_deposit_id_are_rejected(self):
        d=self.deposit();uid=self.uid+4000000;db.ensure_user_id(uid);rs=rdb.create_reseller('Other',uid,'other');rdb.set_reseller_status(rs['id'],'active');other=self.deposit(uid=uid)
        for fn in [lambda:deposits.session(self.uid,other['id']),lambda:deposits.cancel_session(self.uid,other['id'])]:
            with self.assertRaises(deposits.DepositError):fn()
        db.cur.execute('UPDATE users SET blocked=1 WHERE user_id=?',(self.uid,));db.conn.commit()
        with self.assertRaises(deposits.DepositError):deposits.session(self.uid,d['id'])
    def test_unsupported_method_rejected_without_invoice(self):
        self.callback('res_topup_method_creditcard');self.assertEqual(self.scalar('SELECT COUNT(*) FROM reseller_deposits WHERE reseller_id=?',self.rid),0)
        with self.assertRaises(deposits.DepositError):deposits.create_session(self.uid,'forged-network','10')
    def test_amount_validation_no_callback_authority(self):
        for amount in ['NaN','Infinity','0','0.09','5000.01','10.001','<b>10</b>']:
            with self.assertRaises(deposits.DepositError):deposits.create_session(self.uid,'trc20',amount)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM reseller_deposits WHERE reseller_id=?',self.rid),0)
    def test_unique_amount_shared_scope_and_memo_conventions(self):
        # Earlier cases leave valid invoices in this suite-owned database.
        # Choose a clear band before injecting the intended duplicate/nearby
        # collisions; hardcoded 1234 could exhaust the finite mock on unrelated
        # random invoices. All production allocation/precision guards stay on.
        db.cur.execute('SELECT unique_amount FROM deposits UNION ALL SELECT expected_amount FROM reseller_deposits')
        reserved=[shared.Decimal(str(row[0])) for row in db.cur.fetchall() if row[0] is not None]
        base=next(n for n in range(1000,9997) if all(
            abs(shared.Decimal(10)+shared.Decimal(n+offset)/1000000-value)>2*shared.TOL
            for offset in (0,1,3) for value in reserved))
        with patch.object(shared.random,'randint',side_effect=[base,base,base+1,base+3]):
            _,cid,amount,_=self.customer();d=self.deposit()
        self.assertGreater(abs(d['expected_amount']-amount),2*float(shared.TOL));self.assertEqual(round(d['expected_amount'],6),d['expected_amount'])
        self.credit(d);b=self.deposit('binance');self.assertRegex(b['memo'],r'^[A-Z0-9]{8}$');self.assertEqual(b['expected_amount'],b['amount'])
    def test_cancelled_amount_is_not_reassigned_during_provider_lookback(self):
        d=self.deposit();deposits.cancel_session(self.uid,d['id'])
        uid=self.uid+5000000;db.ensure_user_id(uid);rs=rdb.create_reseller('New invoice owner',uid,'new-owner');rdb.set_reseller_status(rs['id'],'active')
        suffix=int(round((d['expected_amount']-d['amount'])*1000000))
        with patch.object(shared.random,'randint',side_effect=[suffix,1000 if abs(suffix-1000)>2 else 2000]):new=self.deposit(uid=uid)
        self.assertGreater(abs(new['expected_amount']-d['expected_amount']),2*float(shared.TOL))
        asyncio.run(deposits.process_observations(self.context,'trc20',[(self.tx(),d['expected_amount'],'')]))
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),0);self.assertEqual(rdb.get_wallet_balance_cents(rs['id']),0)
        self.assertEqual(deposits.row(new['id'])['status'],'pending')
    def test_saved_server_destination_expiry_and_qr(self):
        d=self.deposit();self.assertEqual(d['address'],deposits.destination('trc20'));self.assertEqual(d['status'],'pending');self.assertGreater(d['expires_at'],now_text())
        qr=handlers.reseller_topup_qr(d);self.assertEqual(Image.open(qr).format,'PNG');self.assertGreater(len(qr.getvalue()),100)
        text,markup=handlers.build_reseller_topup_invoice(d);self.assertIn(d['address'],text);self.assertIn(f'{d["expected_amount"]:.6f}',text);self.assertIn('Remaining:',text)
        self.assertTrue(all(b.callback_data.startswith('res_') for row in markup.inline_keyboard for b in row))
    def test_session_reuse_original_wins_and_conflicting_method(self):
        d=self.deposit();again=self.deposit(amount='99');self.assertEqual(again['id'],d['id']);self.assertEqual(again['amount'],10)
        with self.assertRaises(deposits.DepositError):self.deposit('bep20')
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM reseller_deposits WHERE reseller_id=? AND status='pending'",self.rid),1)
    def test_concurrent_session_creation_one_active(self):
        out=self.race([lambda:self.deposit()]*5);self.assertEqual(len({x['id'] for x in out}),1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM reseller_deposits WHERE reseller_id=? AND status='pending'",self.rid),1)
    def test_cancel_clears_navigation_and_preserves_record(self):
        d=self.deposit();user_state[self.uid]=RES_TOPUP_PAYMENT;self.callback(f'res_topup_cancel_{d["id"]}')
        self.assertNotIn(self.uid,user_state);self.assertEqual(deposits.row(d['id'])['status'],'cancelled')
        with self.assertRaises(deposits.DepositError):deposits.cancel_session(self.uid,d['id'])
    def test_cancel_cannot_undo_paid_and_race_is_safe(self):
        d=self.deposit();self.credit(d)
        with self.assertRaises(deposits.DepositError):deposits.cancel_session(self.uid,d['id'])
        self.assertEqual(rdb.get_wallet_balance_cents(self.rid),1000);self.assert_clean()
        d2=self.deposit()
        def cancel():
            try:return deposits.cancel_session(self.uid,d2['id'])
            except deposits.DepositError:return False
        out=self.race([cancel,lambda:self.credit(d2,tx=self.tx(n=self.uid+1))]);saved=deposits.row(d2['id'])
        self.assertIn(saved['status'],('paid','cancelled'));self.assertEqual(rdb.get_wallet_balance_cents(self.rid),2000 if saved['status']=='paid' else 1000);self.assert_clean()
    def test_notifications_full_reference_balance_once_and_owner_action(self):
        d=self.deposit();self.credit(d);asyncio.run(deposits.post_credit_actions(self.context));asyncio.run(deposits.post_credit_actions(self.context))
        calls=[v for v in self.context.bot.send_message.await_args_list if v.kwargs.get('chat_id')==self.uid];self.assertEqual(len(calls),1)
        text=calls[0].kwargs['text'];self.assertIn(self.tx(),text);self.assertIn('dep.trc20.'+self.tx(),text);self.assertIn('10.00',text)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM admin_logs WHERE action='reseller_deposit_credit' AND details LIKE ?",f'%deposit={d["id"]} %'),1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM reseller_security_events WHERE reseller_id=? AND event_type='reseller_deposit_credited'",self.rid),1)
    def test_notification_failure_cannot_reverse_payment(self):
        d=self.deposit();self.credit(d);self.context.bot.send_message=AsyncMock(side_effect=RuntimeError('synthetic telegram failure'))
        asyncio.run(deposits.post_credit_actions(self.context));asyncio.run(deposits.post_credit_actions(self.context))
        self.assertEqual(deposits.row(d['id'])['status'],'paid');self.assertEqual(deposits.row(d['id'])['notification_status'],'attempted');self.assertEqual(rdb.get_wallet_balance_cents(self.rid),1000);self.assert_clean()
    def test_owner_action_failure_retries_without_duplicate_credit(self):
        d=self.deposit();self.credit(d)
        with patch.object(db,'write_admin_action',side_effect=RuntimeError('synthetic audit outage')):asyncio.run(deposits.post_credit_actions(self.context))
        self.assertEqual(deposits.row(d['id'])['audit_logged'],0)
        asyncio.run(deposits.post_credit_actions(self.context));self.assertEqual(deposits.row(d['id'])['audit_logged'],1);self.assertEqual(rdb.get_wallet_balance_cents(self.rid),1000)
    def test_expiry_persists_despite_render_rejection_and_retains_evidence(self):
        d=self.deposit();db.cur.execute("UPDATE reseller_deposits SET expires_at='2000-01-01 00:00:00' WHERE id=?",(d['id'],));db.conn.commit()
        user_state[self.uid]=RES_TOPUP_PAYMENT
        self.callback(f'res_topup_show_{d["id"]}');self.assertEqual(deposits.row(d['id'])['status'],'expired');self.assertNotIn(self.uid,user_state)
        asyncio.run(deposits.process_observations(self.context,'trc20',[(self.tx(),d['expected_amount'],'')]))
        asyncio.run(deposits.process_observations(self.context,'trc20',[(self.tx(),d['expected_amount'],'')]))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM reseller_security_events WHERE reseller_id=? AND event_type='reseller_deposit_late_review'",self.rid),1);self.assertEqual(rdb.get_wallet_balance_cents(self.rid),0)
    def test_restart_recovery_and_confirmed_not_payable(self):
        d=self.deposit();user_state.clear();handlers._topup_drafts.clear();q,edit=self.callback('res_topup');self.assertIn(f'#{d["id"]}',edit.await_args.args[1])
        self.assertEqual(self.deposit()['id'],d['id']);self.credit(d);user_state.clear();self.assertIsNone(deposits.session(self.uid));self.assertEqual(deposits.row(d['id'])['status'],'paid')
    def test_pending_is_processed_without_in_memory_state_after_suspension(self):
        from payments import trc20_checker as checker
        d=self.deposit();user_state.pop(self.uid,None);rdb.set_reseller_status(self.rid,'suspended')
        with patch.object(checker,'_config_ready',return_value=True),patch.object(checker,'_recent_incoming_transfers',new=AsyncMock(return_value=[(self.tx(),d['expected_amount'])])):asyncio.run(checker.check_trc20_payments(self.context))
        self.assertEqual(deposits.row(d['id'])['status'],'paid');self.assertEqual(rdb.get_wallet_balance_cents(self.rid),1000);self.assert_clean()
    def test_checker_expiry_cleans_payment_navigation_once(self):
        d=self.deposit();user_state[self.uid]=RES_TOPUP_PAYMENT
        db.cur.execute("UPDATE reseller_deposits SET expires_at='2000-01-01 00:00:00' WHERE id=?",(d['id'],));db.conn.commit()
        asyncio.run(deposits.process_observations(self.context,'trc20',[]));asyncio.run(deposits.process_observations(self.context,'trc20',[]))
        self.assertNotIn(self.uid,user_state);self.assertEqual(deposits.row(d['id'])['status'],'expired')
    def test_committed_credit_audit_recovers_when_rail_configuration_removed(self):
        from payments import trc20_checker as checker
        d=self.deposit();self.credit(d)
        with patch.object(checker,'_config_ready',return_value=False):asyncio.run(checker.check_trc20_payments(self.context))
        self.assertEqual(deposits.row(d['id'])['audit_logged'],1);self.assertEqual(deposits.row(d['id'])['notification_status'],'attempted')
    def test_provider_error_logging_does_not_leak_credentials(self):
        from payments import binance_checker as checker
        import contextlib,io
        self.deposit('binance');secret='SYNTHETIC-PAYMENT-SECRET-NEVER-LOG'
        stdout=io.StringIO()
        with patch.object(checker,'_config_ready',return_value=True),patch.object(checker,'_recent_pay_transactions',new=AsyncMock(side_effect=RuntimeError(secret))),contextlib.redirect_stdout(stdout),self.assertLogs('oggystore_bot',level='ERROR') as captured:
            asyncio.run(checker.check_binance_payments(self.context))
        self.assertNotIn(secret,stdout.getvalue()+str(captured.output))
    def test_html_escape_and_binance_qr_is_not_payment_token(self):
        d=self.deposit('binance');mutated=dict(d,address='<bad&address>',memo='<memo&>');text,markup=handlers.build_reseller_topup_invoice(mutated)
        self.assertIn('&lt;bad&amp;address&gt;',text);self.assertIn('&lt;memo&amp;&gt;',text);self.assertNotIn('<bad&address>',text)
        self.assertIn('Binance',text)
    def test_customer_invoice_helpers_and_checker_schedule_guards(self):
        root=Path(__file__).resolve().parents[1]
        # Hashes are captured from the verified Phase 16 checkpoint; no second
        # checkout/invoice implementation or changed polling schedule is allowed.
        hashes=json.loads((root/'PHASE17_COMPATIBILITY_HASHES.json').read_text())
        for name,digest in hashes['files'].items():self.assertEqual(hashlib.sha256((root/name).read_bytes()).hexdigest(),digest,name)
        text=(root/'payments/deposit_handlers.py').read_text();tree=ast.parse(text)
        for fn,digest in hashes['customer_functions'].items():
            node=next(x for x in tree.body if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef)) and x.name==fn)
            self.assertEqual(hashlib.sha256(ast.get_source_segment(text,node).encode()).hexdigest(),digest,fn)
        wallet,markup=handlers.build_reseller_wallet(self.rs);callbacks=[b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn('res_topup',callbacks);self.assertIn('res_wallet',callbacks);self.assertIn('res_center',callbacks)

# Avoid inherited tests or imported fixture modules being counted twice.
def load_tests(loader,tests,pattern):
    suite=unittest.TestSuite()
    for name in sorted(n for n in TopupFlowTests.__dict__ if n.startswith('test_')):suite.addTest(TopupFlowTests(name))
    return suite
if __name__=='__main__':unittest.main()
