"""Phase 16.3: real owner route/engine, temp SQLite; Telegram boundary only mocked."""
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

# Reuse the existing project's isolated DB + real API order fixture, not
# financial mocks. Import the module, not its TestCase (avoid extra discovery).
import tests.test_phase16_admin_fail_order as support
import reseller_api.services.order_failure as engine
from reseller_api.services.adjustment_service import issue_admin_refund
from reseller_api.services.statement_service import generate_statement
from reseller_api.services.reconciliation import reconcile_reseller
from utils.timezone import now_text

db, rdb, adb = support.db, support.rdb, support.adb
ROOT = Path(__file__).resolve().parents[1]


class Phase16AdminCancelOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.Phase16AdminFailOrderTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        support.Phase16AdminFailOrderTests.tearDownClass()

    def setUp(self):
        self.fx = support.Phase16AdminFailOrderTests('test_successful_fail_full_effects')
        self.owner = self.fx._owner()
        self.notify = patch('web_admin.app.tg_send', return_value=(True, 'ok'))
        self.send = self.notify.start()
        self.addCleanup(self.notify.stop)

    def pending(self, manual=False, telegram=True):
        if not manual:
            rs, oid, pid = self.fx._make_reseller_order(telegram=telegram)
            # Existing code-stock fixture reserves real stock. Represent an
            # undelivered legacy/in-flight order without changing its debit.
            db.cur.execute("UPDATE orders SET delivery='' WHERE id=?", (oid,))
            db.cur.execute("DELETE FROM webhook_outbox WHERE aggregate_id=? AND event_type='delivery.completed'",
                           (f'ORD-{oid:08d}',))
            db.conn.commit()
            return rs, oid, pid
        n = support._n()
        rs = rdb.create_reseller(f'P16C{n}', 9580000+n, f'p16c{n}')
        rdb.set_reseller_status(rs['id'], 'active')
        key, secret = rdb.create_api_key(rs['id'], 'test')
        db.cur.execute("INSERT INTO products(name,price,stock,delivery,active) VALUES(?,10.0,3,'',1)",
                       (f'Manual cancel {n}',))
        db.conn.commit(); pid = db.cur.lastrowid
        rdb.set_product_access(rs['id'], pid, True)
        rdb.import_reseller_product(rs['id'], pid)
        pub = rdb.ensure_product_public_id(pid)
        rdb.post_wallet_transaction(rs['id'], 'topup_credit', 50000, f'P16C.seed.{n}', 'seed')
        response = self.fx.client.post('/api/v1/orders', headers=support._hdr(key, secret),
                                       json={'product_id':pub,'quantity':1,'client_order_id':f'p16c-{n}'})
        self.assertEqual(response.status_code, 201, response.get_json())
        oid = int(response.get_json()['data']['order_id'].replace('ORD-', ''))
        return rs, oid, pid

    def cancel(self, oid, reason='owner cancellation', client=None):
        return (client or self.owner).post(f'/api/orders/{oid}/cancel', json={'reason':reason},
                                           headers={'X-CSRF-Token':'test-csrf'})

    def scalar(self, sql, params=()):
        db.cur.execute(sql, params)
        return db.cur.fetchone()[0]

    def test_allowed_path_status_refund_stock_webhook_audit(self):
        rs, oid, pid = self.pending()
        response = self.cancel(oid)
        self.assertEqual(response.status_code, 200, response.get_json())
        body=response.get_json()
        self.assertEqual(body['status'], 'cancelled')
        self.assertEqual(body['refunded_cents'], 1000)
        self.assertEqual(body['balance'], 50000)
        self.assertTrue(body['stock_restored'])
        self.assertIn('Cancelled', self.scalar('SELECT master_status FROM orders WHERE id=?',(oid,)))
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=1',(pid,)),0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM webhook_outbox WHERE aggregate_id=? AND event_type='order.cancelled'",(f'ORD-{oid:08d}',)),1)
        rec=adb.get_refund_by_ledger_reference(f'rapi.cancel_refund.order.{oid}')
        self.assertEqual((rec['source'],rec['created_by'],rec['reason']),('admin','owner','owner cancellation'))
        self.assertGreater(self.scalar("SELECT COUNT(*) FROM admin_logs WHERE action LIKE '%reseller_order_cancel%'"),0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM reseller_security_events WHERE reseller_id=? AND event_type='admin_order_cancel'",(rs['id'],)),1)
        self.assertEqual(reconcile_reseller(rs['id']), [])

    def test_manual_pending_restores_counter_once(self):
        rs,oid,pid=self.pending(manual=True)
        self.assertEqual(self.scalar('SELECT stock FROM products WHERE id=?',(pid,)),2)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM webhook_outbox WHERE aggregate_id=? AND event_type='delivery.pending'",(f'ORD-{oid:08d}',)),1)
        self.assertEqual(self.cancel(oid).status_code,200)
        self.assertEqual(self.cancel(oid).status_code,409)
        self.assertEqual(self.scalar('SELECT stock FROM products WHERE id=?',(pid,)),3)
        self.assertEqual(reconcile_reseller(rs['id']),[])

    def test_delivered_rejected_and_unchanged(self):
        rs,oid,pid=self.fx._make_reseller_order()
        balance=rdb.get_wallet_balance_cents(rs['id'])
        response=self.cancel(oid)
        self.assertEqual(response.status_code,409)
        self.assertIn('fail/refund',response.get_json()['error'])
        self.assertEqual(rdb.get_wallet_balance_cents(rs['id']),balance)
        self.assertEqual(adb.list_refunds(order_id=oid),[])
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=1',(pid,)),1)
        self.send.assert_not_called()

    def test_repeated_cancel_one_ledger_record_event(self):
        rs,oid,pid=self.pending()
        self.assertEqual(self.cancel(oid).status_code,200)
        self.assertEqual(self.cancel(oid).status_code,409)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM reseller_wallet_transactions WHERE reference=?",(f'rapi.cancel_refund.order.{oid}',)),1)
        self.assertEqual(len(adb.list_refunds(order_id=oid)),1)
        self.assertEqual(self.send.call_count,1)

    def test_non_owner_forbidden(self):
        rs,oid,pid=self.pending()
        self.assertEqual(self.cancel(oid,client=self.fx._owner(role='admin')).status_code,403)
        self.assertEqual(rdb.get_wallet_balance_cents(rs['id']),49000)

    def test_unauthenticated_denied(self):
        import web_admin.app as wa
        rs,oid,pid=self.pending()
        response=wa.app.test_client().post(f'/api/orders/{oid}/cancel',json={'reason':'x'})
        self.assertIn(response.status_code,(401,403))
        self.assertEqual(adb.list_refunds(order_id=oid),[])

    def test_csrf_denied(self):
        rs,oid,pid=self.pending()
        self.assertEqual(self.owner.post(f'/api/orders/{oid}/cancel',json={'reason':'x'}).status_code,403)

    def test_invalid_reason_matrix(self):
        rs,oid,pid=self.pending()
        for body in ({},{'reason':''},{'reason':'   '},{'reason':501*'x'},
                     {'reason':100},{'reason':{}},{'reason':[]},[],None):
            with self.subTest(body=body):
                self.assertEqual(self.owner.post(f'/api/orders/{oid}/cancel',json=body,
                    headers={'X-CSRF-Token':'test-csrf'}).status_code,400)
        self.assertEqual(rdb.get_wallet_balance_cents(rs['id']),49000)

    def test_reason_boundary_500(self):
        rs,oid,pid=self.pending()
        self.assertEqual(self.cancel(oid,500*'x').status_code,200)

    def test_unknown_order(self):
        self.assertEqual(self.cancel(99999999).status_code,404)

    def test_customer_unchanged(self):
        db.cur.execute("INSERT INTO orders(user_id,product_name,price,delivery) VALUES(7,'customer',10,'customer delivery')")
        db.conn.commit();oid=db.cur.lastrowid
        self.assertEqual(self.cancel(oid).status_code,400)
        self.assertEqual(self.scalar('SELECT delivery FROM orders WHERE id=?',(oid,)),'customer delivery')

    def test_partial_admin_refund_cap_and_notification_actual_amount(self):
        rs,oid,pid=self.pending()
        issue_admin_refund(oid,400,'partial',actor='owner')
        response=self.cancel(oid,'<cancel & safely>')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.get_json()['refunded_cents'],600)
        self.assertEqual(rdb.get_wallet_balance_cents(rs['id']),50000)
        text=self.send.call_args.args[1]
        self.assertIn('6.00',text);self.assertIn('&lt;cancel &amp; safely&gt;',text)
        self.assertNotIn('<cancel & safely>',text)
        self.assertEqual(reconcile_reseller(rs['id']),[])

    def test_full_admin_refund_still_cancels_and_restores(self):
        rs,oid,pid=self.pending(manual=True)
        issue_admin_refund(oid,1000,'already refunded',actor='owner')
        response=self.cancel(oid)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.get_json()['refunded_cents'],0)
        self.assertEqual(self.scalar('SELECT stock FROM products WHERE id=?',(pid,)),3)
        self.assertEqual(reconcile_reseller(rs['id']),[])

    def test_cancel_vs_fail_single_terminal_winner(self):
        rs,oid,pid=self.pending(manual=True)
        barrier=threading.Barrier(2)
        results=[];errors=[]
        def run(action):
            try:
                client=self.fx._owner()
                barrier.wait(timeout=10)
                response=client.post(f'/api/orders/{oid}/{action}',json={'reason':'race'},
                                     headers={'X-CSRF-Token':'test-csrf'})
                results.append((action,response.status_code))
            except Exception as exc:errors.append(type(exc).__name__)
            finally:db.close_thread_connection()
        threads=[threading.Thread(target=run,args=(a,)) for a in ('cancel','fail')]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=20)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors,[])
        self.assertEqual(sorted(code for _,code in results),[200,409],results)
        self.assertEqual(rdb.get_wallet_balance_cents(rs['id']),50000)
        self.assertEqual(self.scalar('SELECT stock FROM products WHERE id=?',(pid,)),3)
        self.assertEqual(len(adb.list_refunds(order_id=oid)),1)
        events=[r[0] for r in db.cur.execute("SELECT event_type FROM webhook_outbox WHERE aggregate_id=? AND event_type IN ('order.failed','order.cancelled')",(f'ORD-{oid:08d}',)).fetchall()]
        self.assertEqual(len(events),1)
        self.assertEqual(reconcile_reseller(rs['id']),[])

    def test_cancelled_then_fail_conflicts(self):
        rs,oid,pid=self.pending()
        self.assertEqual(self.cancel(oid).status_code,200)
        self.assertEqual(self.fx._fail(self.owner,oid).status_code,409)
        self.assertEqual(len(adb.list_refunds(order_id=oid)),1)

    def test_failed_then_cancel_conflicts(self):
        rs,oid,pid=self.pending()
        self.assertEqual(self.fx._fail(self.owner,oid).status_code,200)
        self.assertEqual(self.cancel(oid).status_code,409)

    def test_statement_totals_ledger_and_sales_classification(self):
        rs,oid,pid=self.pending()
        self.assertEqual(self.cancel(oid).status_code,200)
        today=now_text()[:10]
        st=generate_statement(rs['id'],period_type='custom',start_date=today,end_date=today)
        self.assertEqual(st['refunds_cents'],1000)
        self.assertEqual(st['purchases_cents'],1000)  # historical debit is not erased
        self.assertEqual(st['closing_balance_cents'],50000)
        net=self.scalar('SELECT SUM(amount_cents) FROM reseller_wallet_transactions WHERE reseller_id=?',(rs['id'],))
        self.assertEqual(st['closing_balance_cents'],net)
        self.assertEqual(st['orders_count'],1)  # existing ledger historical-purchase count
        sales=rdb.get_reseller_sales_analytics(rs['id'],days=30)
        self.assertEqual(sales['gross_sales_cents'],1000)  # historical charges preserved
        self.assertEqual(sales['net_sales_cents'],0)
        self.assertEqual(sales['quantity_sold'],0)
        self.assertEqual(reconcile_reseller(rs['id']),[])

    def test_telegram_failure_financial_success(self):
        rs,oid,pid=self.pending()
        self.send.side_effect=RuntimeError('synthetic Telegram outage')
        response=self.cancel(oid)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.get_json()['notify'],'failed')
        self.assertEqual(rdb.get_wallet_balance_cents(rs['id']),50000)
        self.assertEqual(reconcile_reseller(rs['id']),[])

    def test_missing_telegram_skips(self):
        rs,oid,pid=self.pending(telegram=False)
        response=self.cancel(oid)
        self.assertEqual(response.get_json()['notify'],'no_telegram_link')
        self.send.assert_not_called()

    def test_cancel_crash_resume_through_route(self):
        rs,oid,pid=self.pending(manual=True)
        with patch.object(engine,'_issue_refund',side_effect=RuntimeError('synthetic crash')):
            self.assertEqual(self.cancel(oid).status_code,500)
        self.assertEqual(rdb.get_wallet_balance_cents(rs['id']),49000)
        response=self.cancel(oid)
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(response.get_json()['result'],'recovered')
        self.assertEqual(self.cancel(oid).status_code,409)
        self.assertEqual(self.scalar('SELECT stock FROM products WHERE id=?',(pid,)),3)
        self.assertEqual(reconcile_reseller(rs['id']),[])

    def test_legacy_metadata_refused(self):
        rs,oid,pid=self.pending()
        db.cur.execute('UPDATE orders SET unit_price_cents=NULL WHERE id=?',(oid,));db.conn.commit()
        self.assertEqual(self.cancel(oid).status_code,422)
        self.assertEqual(adb.list_refunds(order_id=oid),[])

    def test_list_ui_eligibility_and_server_amount(self):
        rs,oid,pid=self.pending()
        delivered=self.fx._make_reseller_order()[1]
        response=self.owner.get('/api/orders?source=reseller_api&per_page=100')
        rows={r['id']:r for r in response.get_json()['orders']}
        self.assertTrue(rows[oid]['can_cancel'])
        self.assertFalse(rows[delivered]['can_cancel'])
        self.assertTrue(rows[delivered]['has_delivery'])
        self.assertEqual(rows[oid]['outstanding_refundable_cents'],1000)
        self.cancel(oid)
        rows={r['id']:r for r in self.owner.get('/api/orders?source=reseller_api&per_page=100').get_json()['orders']}
        self.assertFalse(rows[oid]['can_cancel'])
        self.assertIn('Cancelled',rows[oid]['status_label'])
        js=(ROOT/'web_admin/static/js/app.js').read_text()
        self.assertIn('cancelOrderModal(',js);self.assertIn('o.can_cancel && !o.has_delivery',js)

    def test_delivery_change_between_read_and_claim_is_rejected(self):
        from utils.crypto import encrypt_text
        rs,oid,pid=self.pending()
        original=engine._mark_order_cancelled
        def raced_delivery(*args,**kwargs):
            db.cur.execute('UPDATE orders SET delivery=? WHERE id=?',
                           (encrypt_text('real delivery arrived'),oid))
            db.conn.commit()
            return original(*args,**kwargs)
        with patch.object(engine,'_mark_order_cancelled',side_effect=raced_delivery):
            response=self.cancel(oid)
        self.assertEqual(response.status_code,409)
        self.assertIn('fail/refund',response.get_json()['error'])
        self.assertEqual(adb.list_refunds(order_id=oid),[])
        self.assertEqual(rdb.get_wallet_balance_cents(rs['id']),49000)

    def test_missing_encryption_key_fails_closed_for_existing_ciphertext(self):
        import os
        rs,oid,pid=self.fx._make_reseller_order()
        with patch.dict(os.environ,{'DATA_ENCRYPTION_KEY':''}):
            self.assertEqual(self.cancel(oid).status_code,409)
        self.assertEqual(adb.list_refunds(order_id=oid),[])

    def test_stock_crash_resume_and_partial_refund_attribution(self):
        rs,oid,pid=self.pending(manual=True)
        issue_admin_refund(oid,400,'partial',actor='owner')
        with patch.object(engine,'_restore_stock',side_effect=RuntimeError('stock interrupted')):
            self.assertEqual(self.cancel(oid).status_code,500)
        self.assertEqual(rdb.get_wallet_balance_cents(rs['id']),50000)
        response=self.cancel(oid)
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(response.get_json()['refunded_cents'],600)
        self.assertTrue(response.get_json()['stock_restored'])
        self.assertEqual(self.scalar('SELECT stock FROM products WHERE id=?',(pid,)),3)
        rec=adb.get_refund_by_ledger_reference(f'rapi.cancel_refund.order.{oid}')
        self.assertEqual((rec['source'],rec['created_by']),('admin','owner'))
        self.assertEqual(len(adb.list_refunds(order_id=oid)),2)
        self.assertEqual(self.cancel(oid).status_code,409)
        self.assertEqual(reconcile_reseller(rs['id']),[])

    def test_concurrent_same_cancel_conflicts_while_first_refund_runs(self):
        rs,oid,pid=self.pending(manual=True)
        entered=threading.Event();release=threading.Event();responses=[]
        original=engine._issue_refund
        def paused_refund(*args,**kwargs):
            entered.set()
            if not release.wait(timeout=10):raise RuntimeError('test coordination timeout')
            return original(*args,**kwargs)
        def first():
            try:responses.append(self.cancel(oid,client=self.fx._owner()))
            finally:db.close_thread_connection()
        with patch.object(engine,'_issue_refund',side_effect=paused_refund):
            thread=threading.Thread(target=first);thread.start()
            try:
                self.assertTrue(entered.wait(timeout=10))
                second=self.cancel(oid)
                self.assertEqual(second.status_code,409,second.get_json())
                self.assertIn('in progress',second.get_json()['error'])
            finally:release.set();thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(responses[0].status_code,200,responses[0].get_json())
        self.assertEqual(rdb.get_wallet_balance_cents(rs['id']),50000)
        self.assertEqual(len(adb.list_refunds(order_id=oid)),1)
        self.assertEqual(self.scalar('SELECT stock FROM products WHERE id=?',(pid,)),3)

    def test_stale_admin_claim_recovers_after_process_death(self):
        import time
        rs,oid,pid=self.pending()
        key=f'admin-order-lifecycle:{oid}'
        stamp=int(time.time())-301
        db.cur.execute('INSERT INTO worker_jobs(job_key,worker_id,claimed_at,heartbeat_at) VALUES(?,?,?,?)',
                       (key,'dead-process',stamp,stamp));db.conn.commit()
        self.assertEqual(self.cancel(oid).status_code,200)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM worker_jobs WHERE job_key=?',(key,)),0)

    def test_terminal_claim_and_event_rollback_together(self):
        rs,oid,pid=self.pending()
        with patch.object(engine,'emit_event',side_effect=RuntimeError('outbox insert failed')):
            self.assertEqual(self.cancel(oid).status_code,500)
        self.assertFalse(self.scalar('SELECT master_status FROM orders WHERE id=?',(oid,)))
        self.assertEqual(rdb.get_wallet_balance_cents(rs['id']),49000)
        self.assertEqual(self.cancel(oid).status_code,200)


if __name__=='__main__':
    unittest.main()
