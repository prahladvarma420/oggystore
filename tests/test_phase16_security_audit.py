"""Phase 16 executable cross-boundary audit; real routes, ledger and outbox.

Only provider transport/browser and fault-injection boundaries are mocked.
Existing phase fixtures provide isolated temporary SQLite databases and a
certificate-verified HTTPS supplier stub, never a production localhost bypass.
"""
import json
import threading
import contextlib
import unittest
from unittest.mock import patch
import tests.test_phase16_email_products as email
import tests.test_phase16_fulfillment_wiring as f
from reseller_api.services.order_status import api_order_status

class Phase16SecurityAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): f.Phase16FulfillmentWiringTests.setUpClass()
    @classmethod
    def tearDownClass(cls): f.Phase16FulfillmentWiringTests.tearDownClass()
    def setUp(self):
        self.e=email.Phase16EmailProductsTests('test_m365_pending_then_real_endpoint_completion')
        self.e.setUp(); self.addCleanup(self.e.doCleanups)
        self.fx=self.e.fx; self.owner=self.fx.owner
    def pending(self):
        fixture=self.fx.product(ftype='manual')
        f.db.cur.execute("UPDATE products SET delivery='' WHERE id=?",(fixture[1],));f.db.conn.commit()
        oid,data=self.fx.create(fixture)
        self.assertEqual(data['status'],'processing')
        return fixture,oid
    def action(self,oid,action,client=None,**body):
        return (client or self.owner).post(f'/api/orders/{oid}/{action}',json=body or {'reason':'audit action'},headers={'X-CSRF-Token':'test-csrf'})
    def clean(self,fixture):self.assertEqual(f.reconcile_reseller(fixture[0]['id']),[])
    def test_owner_fail_reason_cannot_export_private_buyer_email(self):
        self.reason_privacy('fail')
    def test_owner_cancel_reason_cannot_export_private_buyer_email(self):
        self.reason_privacy('cancel')
    def reason_privacy(self,action):
        fixture,oid,iid=self.e.create()
        with patch('web_admin.app.tg_send',return_value=(True,'ok')) as tg:
            response=self.action(oid,action,reason='Buyer '+email.EMAIL+' requested this action')
        self.assertEqual(response.status_code,200,response.get_json())
        f.db.cur.execute('SELECT payload_json FROM webhook_outbox WHERE aggregate_id=?',(f'ORD-{oid:08d}',))
        events=json.dumps(f.db.cur.fetchall())
        self.assertNotIn(email.EMAIL,events,'PII in actual persisted outbox')
        self.assertNotIn(email.EMAIL,str(tg.call_args_list),'PII in notification')
        self.assertNotIn(email.EMAIL,response.get_data(as_text=True))
        import database.adjustment_db as adb
        self.assertNotIn(email.EMAIL,json.dumps(adb.list_refunds(order_id=oid),default=str))
        audit=f.db.cur.execute('SELECT details FROM admin_logs WHERE details LIKE ?',(f'#{oid} |%',)).fetchall()
        security=f.db.cur.execute('SELECT description FROM reseller_security_events WHERE reseller_id=?',(fixture[0]['id'],)).fetchall()
        self.assertTrue(audit);self.assertTrue(security)
        self.assertNotIn(email.EMAIL,str(audit)+str(security))
        self.clean(fixture)
    def test_whitespace_delivery_roundtrips_through_actual_endpoint(self):
        fixture,oid=self.pending();content=' \n\t'
        response=self.action(oid,'deliver',delivery=content)
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(api_order_status(oid),'completed')
        self.assertEqual(self.fx.fx.read(oid,fixture[3])['items'],[content])
        self.assertEqual(self.fx.events(oid,'delivery.completed'),1);self.clean(fixture)
    def test_separator_only_delivery_is_not_silently_discarded(self):
        fixture,oid=self.pending();content='─'*28
        self.assertEqual(self.action(oid,'deliver',delivery=content).status_code,200)
        self.assertEqual(self.fx.fx.read(oid,fixture[3])['items'],[content]);self.clean(fixture)
    def test_legacy_replacement_corrupt_ciphertext_never_reports_delivered(self):
        fixture,oid=self.pending()
        # Use the actual replacement relation, not a mocked status resolver.
        f.db.cur.execute("INSERT INTO replacements(order_id,user_id,status) VALUES(?,0,'approved')",(oid,))
        f.db.cur.execute("UPDATE orders SET delivery='enc:not-authentic' WHERE id=?",(oid,));f.db.conn.commit()
        self.addCleanup(lambda: self.restore_delivery(oid))
        response=self.fx.api.get(f'/api/v1/orders/ORD-{oid:08d}/delivery',headers=fixture[3])
        self.assertEqual(response.status_code,503,response.get_json())
        self.assertNotIn('[encrypted:',response.get_data(as_text=True))
    def restore_delivery(self,oid):
        f.db.cur.execute('UPDATE orders SET delivery=NULL WHERE id=?',(oid,));f.db.conn.commit()

    def support_owner(self,role='owner'):
        return self.fx.fx.fx.fx._owner(role)
    def test_authorization_matrix_lifecycle_and_secret_config(self):
        fixture,oid=self.pending()
        import web_admin.app as wa
        for client,expected in ((wa.app.test_client(),401),(self.support_owner('admin'),403),(self.support_owner('reseller'),403)):
            for action in ('fail','cancel','deliver'):
                with self.subTest(role=expected,action=action):
                    response=self.action(oid,action,client,**({'delivery':'audit private code'} if action=='deliver' else {'reason':'audit'}))
                    self.assertEqual(response.status_code,expected,response.get_json())
            response=client.post(f'/api/products/{fixture[1]}/update',json={'fulfillment_type':'external_api','fulfillment_config':fixture[5]},headers={'X-CSRF-Token':'test-csrf'})
            self.assertEqual(response.status_code,expected,response.get_json())
        for action in ('fail','cancel','deliver'):
            no_csrf=self.owner.post(f'/api/orders/{oid}/{action}',json={'reason':'audit','delivery':'audit'})
            self.assertEqual(no_csrf.status_code,403)
        no_csrf=self.owner.post(f'/api/products/{fixture[1]}/update',json={'fulfillment_type':'manual'})
        self.assertEqual(no_csrf.status_code,403)
        self.assertEqual(api_order_status(oid),'processing');self.assertEqual(f.rdb.get_wallet_balance_cents(fixture[0]['id']),49000)
        self.assertEqual(self.action(999999999,'fail').status_code,404)
        self.clean(fixture)
    def test_real_api_idor_foreign_order_indistinguishable_from_missing(self):
        fixture,oid=self.pending();foreign=self.fx.product(ftype='manual')
        for suffix in ('','/delivery'):
            a=self.fx.api.get(f'/api/v1/orders/ORD-{oid:08d}'+suffix,headers=foreign[3])
            b=self.fx.api.get('/api/v1/orders/ORD-99999999'+suffix,headers=foreign[3])
            self.assertEqual(a.status_code,404);self.assertEqual(b.status_code,404)
            self.assertEqual(a.get_json()['error'],b.get_json()['error'])
            self.assertNotIn(fixture[0]['public_id'],a.get_data(as_text=True))
        self.clean(fixture)
    def test_owner_does_not_apply_reseller_actions_to_customer_orders(self):
        fixture,oid=self.pending()
        f.db.cur.execute("UPDATE orders SET source='telegram' WHERE id=?",(oid,));f.db.conn.commit()
        for action in ('fail','cancel','deliver'):
            r=self.action(oid,action,**({'delivery':'not allowed'} if action=='deliver' else {'reason':'audit'}))
            self.assertEqual(r.status_code,400,r.get_json())
        self.assertEqual(f.rdb.get_wallet_balance_cents(fixture[0]['id']),49000)
        f.db.cur.execute("UPDATE orders SET source='reseller_api' WHERE id=?",(oid,));f.db.conn.commit();self.clean(fixture)
    def test_partial_refund_cap_terminal_actions_and_statements(self):
        from reseller_api.services.adjustment_service import issue_admin_refund
        from reseller_api.services.statement_service import generate_statement
        from utils.timezone import now_text
        import database.adjustment_db as adb
        for action in ('fail','cancel'):
            fixture,oid=self.pending();issue_admin_refund(oid,400,'partial audit refund',actor='owner')
            with patch('web_admin.app.tg_send',return_value=(True,'ok')):
                response=self.action(oid,action);again=self.action(oid,action)
            self.assertEqual(response.status_code,200);self.assertEqual(response.get_json()['refunded_cents'],600)
            self.assertEqual(again.status_code,409);self.assertEqual(f.rdb.get_wallet_balance_cents(fixture[0]['id']),50000)
            self.assertEqual(sum(x['amount_cents'] for x in adb.list_refunds(order_id=oid)),1000)
            self.assertEqual(self.fx.scalar('SELECT stock FROM products WHERE id=?',(fixture[1],)),10)
            day=now_text()[:10];st=generate_statement(fixture[0]['id'],period_type='custom',start_date=day,end_date=day)
            self.assertEqual(st['refunds_cents'],1000);self.assertEqual(st['closing_balance_cents'],50000)
            self.assertEqual(st['closing_balance_cents'],self.fx.scalar('SELECT SUM(amount_cents) FROM reseller_wallet_transactions WHERE reseller_id=?',(fixture[0]['id'],)))
            self.clean(fixture)
    def test_financial_integrity_hook_rollback_then_crash_resume(self):
        import database.adjustment_db as adb
        fixture,oid=self.pending()
        with patch.object(adb,'insert_refund_record',side_effect=RuntimeError('audit record insertion crash')):
            response=self.action(oid,'fail')
        self.assertEqual(response.status_code,500)
        self.assertEqual(f.rdb.get_wallet_balance_cents(fixture[0]['id']),49000)
        self.assertEqual(adb.list_refunds(order_id=oid),[])
        self.assertEqual(self.fx.events(oid,'refund.created'),0)
        self.assertEqual(self.fx.scalar("SELECT COUNT(*) FROM reseller_wallet_transactions WHERE reference=?",(f'rapi.refund.order.{oid}',)),0)
        # Existing terminal claim commits before refund; retry is required recovery.
        self.assertEqual(api_order_status(oid),'failed')
        with patch('web_admin.app.tg_send',return_value=(True,'ok')):response=self.action(oid,'fail')
        self.assertEqual(response.status_code,200);self.assertEqual(len(adb.list_refunds(order_id=oid)),1)
        self.assertEqual(f.rdb.get_wallet_balance_cents(fixture[0]['id']),50000);self.clean(fixture)
    def test_double_click_and_two_browser_terminal_races(self):
        import database.adjustment_db as adb
        for actions in (('fail','fail'),('cancel','cancel'),('fail','cancel')):
            fixture,oid=self.pending();barrier=threading.Barrier(2);results=[];errors=[]
            def run(action):
                try:
                    client=self.support_owner();barrier.wait(timeout=10)
                    results.append(self.action(oid,action,client).status_code)
                except Exception as exc:errors.append(type(exc).__name__)
                finally:f.db.close_thread_connection()
            with patch('web_admin.app.tg_send',return_value=(True,'ok')):
                threads=[threading.Thread(target=run,args=(a,)) for a in actions]
                for t in threads:t.start()
                for t in threads:t.join(timeout=20)
            self.assertFalse(any(t.is_alive() for t in threads));self.assertEqual(errors,[])
            self.assertEqual(sorted(results),[200,409]);self.assertEqual(len(adb.list_refunds(order_id=oid)),1)
            self.assertEqual(self.fx.scalar('SELECT stock FROM products WHERE id=?',(fixture[1],)),10)
            self.assertEqual(self.fx.events(oid,'order.failed')+self.fx.events(oid,'order.cancelled'),1)
            self.assertEqual(f.rdb.get_wallet_balance_cents(fixture[0]['id']),50000);self.clean(fixture)
    def test_delivery_outbox_rollback_then_retry_exactly_once(self):
        fixture,oid=self.pending()
        with patch('reseller_api.services.webhook_outbox.emit_event_atomic',side_effect=RuntimeError('audit outbox crash')):
            response=self.action(oid,'deliver',delivery='PRIVATE-CODE-audit')
        self.assertEqual(response.status_code,500);self.assertEqual(api_order_status(oid),'processing')
        self.assertIsNone(self.fx.scalar('SELECT delivery FROM orders WHERE id=?',(oid,)))
        self.assertEqual(self.fx.events(oid,'delivery.completed'),0)
        response=self.action(oid,'deliver',delivery='PRIVATE-CODE-audit');self.assertEqual(response.status_code,200)
        repeat=self.action(oid,'deliver',delivery='different');self.assertEqual(repeat.status_code,200)
        self.assertEqual(repeat.get_json()['note'],'delivery already present')
        self.assertEqual(self.fx.fx.read(oid,fixture[3])['items'],['PRIVATE-CODE-audit'])
        self.assertEqual(self.fx.events(oid,'delivery.completed'),1);self.clean(fixture)
    def test_cancel_after_delivery_cannot_restore_manual_stock(self):
        fixture,oid=self.pending();self.assertEqual(self.action(oid,'deliver',delivery='valid code').status_code,200)
        before=self.fx.scalar('SELECT stock FROM products WHERE id=?',(fixture[1],))
        self.assertEqual(self.action(oid,'cancel').status_code,409)
        self.assertEqual(self.fx.scalar('SELECT stock FROM products WHERE id=?',(fixture[1],)),before)
        self.assertEqual(f.rdb.get_wallet_balance_cents(fixture[0]['id']),49000);self.clean(fixture)
    def test_lifecycle_event_allowlists_and_no_delivery_content(self):
        common={'event','event_type','order_id','reseller_id','order_status','delivery_status','timestamp'}
        allowed={
            'order.created':common|{'product_id','quantity','unit_price','total','currency','created_at','client_order_id'},
            'delivery.pending':common|{'product_id','quantity','created_at','note'},
            'delivery.completed':common|{'product_id','quantity','created_at','note'},
            'order.failed':common|{'reason','failed_at'},'delivery.failed':common|{'reason','failed_at'},
            'order.cancelled':common|{'cancelled_at'},
            'refund.created':{'event','event_type','order_id','reseller_id','amount','currency','refund_type','timestamp','created_at'},
        }
        for action in ('fail','cancel','deliver'):
            fixture,oid=self.pending()
            with patch('web_admin.app.tg_send',return_value=(True,'ok')):
                r=self.action(oid,action,**({'delivery':'PRIVATE-CODE-event-audit'} if action=='deliver' else {'reason':'audit'}))
            self.assertEqual(r.status_code,200)
            rows=f.db.cur.execute('SELECT event_type,payload_json FROM webhook_outbox WHERE aggregate_id=?',(f'ORD-{oid:08d}',)).fetchall()
            self.assertTrue(rows)
            for event,raw in rows:
                self.assertIn(event,allowed);self.assertLessEqual(set(json.loads(raw)),allowed[event])
                for sensitive in ('PRIVATE-CODE-event-audit',email.EMAIL,f.SECRET,f.HMAC_SECRET):self.assertNotIn(sensitive,raw)
            self.assertEqual(len(rows),len({ev for ev,_ in rows}));self.clean(fixture)
    def test_phase15_four_way_and_attributed_stock_regressions(self):
        # Established real route/code-attribution fixtures; no refund/stock mocks.
        import tests.test_phase16_admin_cancel_order as c
        case=c.Phase16AdminCancelOrderTests('test_allowed_path_status_refund_stock_webhook_audit')
        case.setUp()
        try:case.test_allowed_path_status_refund_stock_webhook_audit()
        finally:case.doCleanups()
    def test_supplier_ssrf_private_mixed_addresses_and_rebinding(self):
        self.fx.test_private_dns_and_rebinding_no_request()
        self.fx.test_dns_changes_after_save_validation_before_connect()
    def test_supplier_https_timeouts_retry_budgets_and_pinning(self):
        self.fx.test_config_timeout_retry_budget_bounds()
        self.fx.test_pinned_adapter_retains_host_sni_certificate_validation()
    def test_supplier_redirect_never_followed(self):self.fx.test_redirect_never_followed_and_refunded()
    def test_supplier_transient_and_permanent_retry_budget(self):
        self.fx.test_transient_then_success_hmac_and_stable_idempotency()
        self.fx.test_exhausted_retry_limit_refunds_once()
        self.fx.test_terminal_failure_full_refund()
    def test_supplier_network_outside_transaction_and_committed_order(self):self.fx.test_router_entry_is_outside_order_atomic()
    def test_supplier_adversarial_log_and_raw_exception_privacy(self):
        self.fx.test_adversarial_logs_api_outbox_no_secrets()
        self.fx.test_adversarial_transport_exception_is_sanitized()
    def test_preserve_code_stock_manual_and_replay(self):
        self.fx.test_code_stock_unchanged_bytes_no_http();self.fx.test_manual_static_unchanged_no_http()
        self.fx.test_replay_no_second_supplier_request_or_new_response_object()
    def test_private_email_real_logs_exports_events_analytics_responses(self):self.e.test_redaction_scan_real_responses_events_exports_analytics_logs()
    def test_email_queue_crash_completion_retries_without_duplicate_invite(self):self.e.test_sent_receipt_resumes_local_outbox_failure_without_second_invite()
    def test_email_lane_busy_age_failure_and_refund(self):self.e.test_permanently_busy_lane_still_age_fails_refunds()
    def test_email_quotes_no_price_or_side_effect_drift(self):self.e.test_quotes_require_email_flag_and_no_side_effects()
    def test_email_replay_original_private_recipient_wins(self):self.e.test_replay_different_email_keeps_original_ciphertext_and_one_queue_row()
    def test_worker_poll_network_outside_atomic_and_reconciliation_clean(self):
        import tests.test_phase16_supplier_fulfillment as supplier
        from reseller_api.workers import fulfillment_worker as w
        server=f.Phase16FulfillmentWiringTests.server;original=server.RequestHandlerClass
        server.RequestHandlerClass=supplier.SupplierStub
        case=supplier.Phase16SupplierFulfillmentTests('test_real_delivery_endpoint_and_exactly_one_completion');case.setUp()
        try:
            fixture,oid=case.create();case.state(oid,'fulfilled');states=[]
            original_dispatch=w.dispatch_client
            def checked(*a,**kw):
                states.append(f.db.get_conn().in_transaction);return original_dispatch(*a,**kw)
            with patch.object(w,'dispatch_client',side_effect=checked):w._process_order(oid)
            self.assertEqual(states,[False]);case.completed(fixture,oid)
        finally:case.doCleanups();server.RequestHandlerClass=original
    def test_audit_full_mixed_workflow_zero_reconciliation_discrepancies(self):
        touched=[]
        for action in ('cancel','fail','deliver'):
            fixture,oid=self.pending();touched.append(fixture)
            with patch('web_admin.app.tg_send',return_value=(True,'ok')):
                response=self.action(oid,action,**({'delivery':'audit mixed receipt'} if action=='deliver' else {'reason':'audit mixed workflow'}))
            self.assertEqual(response.status_code,200)
        fixture,oid,_=self.e.create();touched.append(fixture)
        with patch('web_admin.app.tg_send',return_value=(True,'ok')):self.assertEqual(self.action(oid,'fail').status_code,200)
        discrepancies={x[0]['id']:f.reconcile_reseller(x[0]['id']) for x in touched}
        self.assertEqual(discrepancies,{x[0]['id']:[] for x in touched})

    def test_non_https_and_localhost_endpoint_rejected_before_http(self):
        self.fx.transport.close()
        for endpoint in ('http://supplier.example/','https://127.0.0.1/','https://[::1]/','https://localhost/','https://169.254.169.254/'):
            with self.subTest(endpoint=endpoint),patch.object(f.generic,'new_session') as network:
                with self.assertRaises(f.generic.FulfillmentConfigError):f.generic.validate_config({'endpoint':endpoint})
                network.assert_not_called()
    def test_terminal_notifications_are_outside_transaction_and_best_effort(self):
        for action in ('fail','cancel'):
            fixture,oid=self.pending();states=[]
            def unavailable(*args,**kwargs):
                states.append(f.db.get_conn().in_transaction);raise RuntimeError('synthetic notification outage')
            with patch('reseller_api.services.order_notifications._enqueue_reseller_order',side_effect=unavailable):response=self.action(oid,action)
            self.assertEqual(response.status_code,200);self.assertEqual(states,[False])
            self.assertEqual(f.rdb.get_wallet_balance_cents(fixture[0]['id']),50000);self.clean(fixture)
    def test_processing_sql_parity_encrypted_empty_real_and_index_bound(self):
        import tests.test_phase16_status_truthfulness as st
        case=st.Phase16StatusTruthfulnessTests('test_creation_null_and_processing_all_read_paths')
        case.setUp()
        try:
            case.test_creation_null_and_processing_all_read_paths()
            case.test_real_short_content_same_cipher_length_preserved()
            case.test_terminal_precedence_and_filter()
            case.test_index_bounded_large_table_explain()
        finally:case.doCleanups()
    def test_terminal_refund_restores_only_attributed_code_stock(self):
        import tests.test_phase16_admin_cancel_order as c
        case=c.Phase16AdminCancelOrderTests('test_allowed_path_status_refund_stock_webhook_audit');case.setUp()
        try:
            rs,oid,pid=case.pending()
            # A different order's sold row is not owned by this cancellation.
            f.db.cur.execute('INSERT INTO product_stock(product_id,delivery,sold,order_id) VALUES(?,?,1,?)',(pid,'OTHER-ORDER-PRIVATE-CODE',9999999));f.db.conn.commit()
            self.assertEqual(case.cancel(oid).status_code,200);self.assertEqual(case.cancel(oid).status_code,409)
            self.assertEqual(self.fx.scalar('SELECT sold FROM product_stock WHERE order_id=?',(9999999,)),1)
            self.assertEqual(self.fx.scalar('SELECT COUNT(*) FROM product_stock WHERE order_id=? AND sold=1',(oid,)),0)
            self.assertEqual(f.reconcile_reseller(rs['id']),[])
        finally:case.doCleanups()

if __name__=='__main__':unittest.main()
