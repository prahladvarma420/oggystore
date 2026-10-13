"""Phase 16.10 — final real-service integration & cumulative regression capstone.

Every Phase 16 feature is exercised through real routes/persistence, including
failure/retry paths. Existing isolated fixtures/assertions are reused explicitly;
no test merely checks that another test file exists. TLS supplier and browser /
Telegram boundaries are the only external-provider substitutes. Prior capstones
run in isolated subprocesses to prevent their module-global DB/sys.modules fixtures
from corrupting this live application fixture. No production data is used.

Execution-status table (populated from the actual final file-by-file run):
Final isolated file-by-file run: 87 modules attempted; 85 runnable suites pass;
1,799 reported cases = 1,798 passed + one pre-existing skip. Two empty modules
exit 5 with zero tests and are NOT passing suites. No runnable failures or
not-run repository test modules. Counts below exclude the 89 repeated child
cases exercised inside this capstone. Normal and reverse capstone order: 29/29
PASS each. Live-provider/deployment checks remain UNVERIFIED.

| Suite | Reported cases | Exit | Actual outcome |
| --- | ---: | ---: | --- |
| `test_abuse_monitoring` | 25 | 0 | PASS |
| `test_api_authentication_service` | 0 | 5 | EMPTY / no tests (not passed) |
| `test_api_authorization_service` | 14 | 0 | PASS |
| `test_api_balance_service` | 4 | 0 | PASS |
| `test_api_endpoint_rate_limit` | 22 | 0 | PASS |
| `test_api_observability_service` | 0 | 5 | EMPTY / no tests (not passed) |
| `test_api_product_catalog_service` | 16 | 0 | PASS |
| `test_api_product_detail_service` | 12 | 0 | PASS |
| `test_api_product_performance` | 5 | 0 | PASS |
| `test_api_product_security_review` | 12 | 0 | PASS |
| `test_api_rate_limit` | 45 | 0 | PASS |
| `test_api_v1_balance` | 7 | 0 | PASS |
| `test_api_v1_delivery` | 6 | 0 | PASS |
| `test_api_v1_foundation` | 18 | 0 | PASS |
| `test_api_v1_products` | 14 | 0 | PASS |
| `test_api_v1_webhooks` | 44 | 0 | PASS |
| `test_auth_abuse_protection` | 20 | 0 | PASS |
| `test_event_generation` | 16 | 0 | PASS |
| `test_financial_abuse_protection` | 19 | 0 | PASS |
| `test_phase10_final` | 92 | 0 | PASS with 1 pre-existing skip |
| `test_phase11_pricing` | 47 | 0 | PASS |
| `test_phase11_product_catalog` | 77 | 0 | PASS |
| `test_phase11_product_customization` | 71 | 0 | PASS |
| `test_phase11_product_import` | 48 | 0 | PASS |
| `test_phase11_stock` | 39 | 0 | PASS |
| `test_phase11_sync` | 48 | 0 | PASS |
| `test_phase11_webhooks` | 53 | 0 | PASS |
| `test_phase12_concurrency_idempotency` | 19 | 0 | PASS |
| `test_phase12_failure_recovery` | 24 | 0 | PASS |
| `test_phase12_final_regression` | 9 | 0 | PASS |
| `test_phase12_order_creation` | 21 | 0 | PASS |
| `test_phase12_order_lifecycle` | 9 | 0 | PASS |
| `test_phase12_order_mapping` | 15 | 0 | PASS |
| `test_phase12_order_security_audit` | 18 | 0 | PASS |
| `test_phase12_quotes` | 13 | 0 | PASS |
| `test_phase12_wallet_integration` | 13 | 0 | PASS |
| `test_phase13_api_security_audit` | 10 | 0 | PASS |
| `test_phase13_documentation` | 8 | 0 | PASS |
| `test_phase13_final_readiness` | 11 | 0 | PASS |
| `test_phase13_key_lifecycle` | 14 | 0 | PASS |
| `test_phase13_reliability` | 11 | 0 | PASS |
| `test_phase13_response_standardization` | 14 | 0 | PASS |
| `test_phase13_sandbox` | 8 | 0 | PASS |
| `test_phase13_usage_monitoring` | 8 | 0 | PASS |
| `test_phase13_versioning` | 9 | 0 | PASS |
| `test_phase14_analytics_audit` | 7 | 0 | PASS |
| `test_phase14_dashboards` | 7 | 0 | PASS |
| `test_phase14_exports` | 9 | 0 | PASS |
| `test_phase14_financial_analytics` | 6 | 0 | PASS |
| `test_phase14_product_analytics` | 4 | 0 | PASS |
| `test_phase14_sales_analytics` | 6 | 0 | PASS |
| `test_phase14_usage_analytics` | 6 | 0 | PASS |
| `test_phase14_webhook_analytics` | 5 | 0 | PASS |
| `test_phase15_adjustments` | 14 | 0 | PASS |
| `test_phase15_final` | 10 | 0 | PASS |
| `test_phase15_financial_security_audit` | 10 | 0 | PASS |
| `test_phase15_invoice_statement_api` | 8 | 0 | PASS |
| `test_phase15_invoices` | 15 | 0 | PASS |
| `test_phase15_ledger_integration` | 12 | 0 | PASS |
| `test_phase15_reconciliation` | 14 | 0 | PASS |
| `test_phase15_statements` | 13 | 0 | PASS |
| `test_phase15_webadmin_financial` | 9 | 0 | PASS |
| `test_phase16_admin_cancel_order` | 28 | 0 | PASS |
| `test_phase16_admin_fail_order` | 10 | 0 | PASS |
| `test_phase16_email_products` | 35 | 0 | PASS |
| `test_phase16_final` | 29 | 0 | PASS |
| `test_phase16_fulfillment_wiring` | 37 | 0 | PASS |
| `test_phase16_manual_delivery` | 27 | 0 | PASS |
| `test_phase16_refund_quarantine` | 5 | 0 | PASS |
| `test_phase16_security_audit` | 33 | 0 | PASS |
| `test_phase16_status_truthfulness` | 31 | 0 | PASS |
| `test_phase16_supplier_fulfillment` | 40 | 0 | PASS |
| `test_request_validation` | 39 | 0 | PASS |
| `test_reseller_api_infrastructure` | 8 | 0 | PASS |
| `test_reseller_quotas` | 15 | 0 | PASS |
| `test_reseller_wallet_concurrency` | 7 | 0 | PASS |
| `test_reseller_wallet_idempotency` | 7 | 0 | PASS |
| `test_reseller_wallet_security_review` | 10 | 0 | PASS |
| `test_reseller_wallet_service` | 9 | 0 | PASS |
| `test_reseller_wallet_web_admin` | 7 | 0 | PASS |
| `test_security_audit` | 63 | 0 | PASS |
| `test_webhook_delivery_worker` | 26 | 0 | PASS |
| `test_webhook_idempotency` | 25 | 0 | PASS |
| `test_webhook_management_api` | 48 | 0 | PASS |
| `test_webhook_outbox` | 29 | 0 | PASS |
| `test_webhook_retry` | 36 | 0 | PASS |
| `test_webhook_signature` | 32 | 0 | PASS |

| Additional capstone child run | Cases | Exit | Actual outcome |
| --- | ---: | ---: | --- |
| `test_phase14_analytics_audit` | 7 | 0 | PASS |
| `test_phase15_final` | 10 | 0 | PASS |
| `test_phase15_financial_security_audit` | 10 | 0 | PASS |
| `test_phase11_webhooks` | 53 | 0 | PASS |
| `test_phase12_final_regression` | 9 | 0 | PASS |

Live PostgreSQL, real Microsoft/Canva/supplier sessions and Railway are unverified.
"""
import contextlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import patch
import yaml
import tests.test_phase16_security_audit as audit
import tests.test_phase16_email_products as email
import tests.test_phase16_supplier_fulfillment as supplier
import tests.test_phase16_admin_cancel_order as cancel
import tests.test_phase16_status_truthfulness as status
import tests.test_phase16_fulfillment_wiring as f
from reseller_api.services.order_status import api_order_status
from utils.crypto import decrypt_text
ROOT=Path(__file__).resolve().parents[1]
HTTP_METHODS={'get','post','put','patch','delete','options','head','trace'}
ADMIN_ENDPOINTS={'api_products','api_orders','api_product_create','api_product_update',
                 'api_order_fail','api_order_cancel','api_order_deliver','api_order_refund','api_order_resend'}

def normalized(path):
    path=re.sub(r'<(?:[^:>]+:)?[^>]+>', '{}',path)
    return re.sub(r'\{[^}]*\}', '{}',path).rstrip('/') or '/'

def operations(spec):
    return {(m.upper(),normalized(p)) for p,item in spec['paths'].items() for m in item if m in HTTP_METHODS}

class Phase16FinalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):f.Phase16FulfillmentWiringTests.setUpClass()
    @classmethod
    def tearDownClass(cls):f.Phase16FulfillmentWiringTests.tearDownClass()
    def setUp(self):
        self.a=audit.Phase16SecurityAuditTests('test_audit_full_mixed_workflow_zero_reconciliation_discrepancies')
        self.a.setUp();self.addCleanup(self.a.doCleanups)
        self.fx=self.a.fx;self.e=self.a.e;self.owner=self.a.owner
    def test_16p1_reseller_quarantine_is_side_effect_free_customer_scope_retained(self):
        fixture,oid=self.a.pending()
        snapshot=(f.rdb.get_wallet_balance_cents(fixture[0]['id']),
            self.fx.scalar('SELECT COUNT(*) FROM orders WHERE id=?',(oid,)),
            self.fx.scalar('SELECT stock FROM products WHERE id=?',(fixture[1],)))
        with patch('web_admin.app.tg_send') as telegram:
            for action in ('refund','resend'):
                response=self.a.action(oid,action)
                self.assertEqual(response.status_code,409,response.get_json())
            telegram.assert_not_called()
        self.assertEqual(snapshot,(f.rdb.get_wallet_balance_cents(fixture[0]['id']),
            self.fx.scalar('SELECT COUNT(*) FROM orders WHERE id=?',(oid,)),
            self.fx.scalar('SELECT stock FROM products WHERE id=?',(fixture[1],))))
        self.a.clean(fixture)
    def test_16p2_fail_success_partial_cap_and_statement_ledger_equality(self):
        self.a.test_partial_refund_cap_terminal_actions_and_statements()
    def test_16p2_fail_transaction_fault_then_exactly_once_recovery(self):
        self.a.test_financial_integrity_hook_rollback_then_crash_resume()
    def test_16p3_cancel_success_attributed_stock_and_repeated_conflict(self):
        self.a.test_terminal_refund_restores_only_attributed_code_stock()
    def test_16p3_delivered_cancel_rejected_without_money_or_stock_changes(self):
        self.a.test_cancel_after_delivery_cannot_restore_manual_stock()
    def test_16p2_16p3_concurrent_terminal_actions_one_winner(self):
        self.a.test_double_click_and_two_browser_terminal_races()
    def test_16p4_pending_manual_delivery_end_to_end_encrypted_and_replay(self):
        fixture,oid=self.a.pending();content='CAPSTONE-PRIVATE-CODE 🧩'
        with patch('fulfillment.order_dispatch.route_fulfillment') as external:
            response=self.a.action(oid,'deliver',delivery=content);external.assert_not_called()
        self.assertEqual(response.status_code,200,response.get_json());self.assertNotIn(content,response.get_data(as_text=True))
        token=self.fx.scalar('SELECT delivery FROM orders WHERE id=?',(oid,))
        self.assertTrue(token.startswith('enc:'));self.assertEqual(decrypt_text(token),content)
        self.assertEqual(self.fx.fx.read(oid,fixture[3])['items'],[content])
        replay=self.a.action(oid,'deliver',delivery='different')
        self.assertEqual(replay.status_code,200);self.assertEqual(replay.get_json()['note'],'delivery already present')
        self.assertEqual(self.fx.scalar('SELECT delivery FROM orders WHERE id=?',(oid,)),token)
        self.assertEqual(self.fx.events(oid,'delivery.completed'),1);self.a.clean(fixture)
    def test_16p4_delivery_validation_terminal_and_atomic_outbox_failure(self):
        fixture,oid=self.a.pending()
        for delivery,code in (('',400),('x'*10001,413),(None,400),(23,400)):
            r=self.a.action(oid,'deliver',delivery=delivery);self.assertEqual(r.status_code,code,r.get_json())
        self.assertIsNone(self.fx.scalar('SELECT delivery FROM orders WHERE id=?',(oid,)))
        self.a.test_delivery_outbox_rollback_then_retry_exactly_once()
        with patch('web_admin.app.tg_send',return_value=(True,'ok')):self.assertEqual(self.a.action(oid,'cancel').status_code,200)
        self.assertEqual(self.a.action(oid,'deliver',delivery='forbidden').status_code,409);self.a.clean(fixture)
    def test_16p5_processing_truth_SQL_encrypted_empty_and_index_bounds(self):
        self.a.test_processing_sql_parity_encrypted_empty_real_and_index_bound()
        case=status.Phase16StatusTruthfulnessTests('test_creation_null_and_processing_all_read_paths');case.setUp()
        try:
            case.test_unreadable_not_guessed_and_cleanup_blocks_readiness()
            case.test_whitespace_is_actual_content_preserved()
        finally:case.doCleanups()
    def test_16p5_processing_to_completed_agrees_list_detail_delivery_export_webadmin(self):
        import csv,io
        fixture,oid=self.a.pending();public=f'ORD-{oid:08d}'
        for expected in ('processing','completed'):
            detail=self.fx.api.get('/api/v1/orders/'+public,headers=fixture[3]);self.assertEqual(detail.status_code,200)
            self.assertEqual(detail.get_json()['data']['order']['status'],expected)
            listing=self.fx.api.get('/api/v1/orders',headers=fixture[3]);self.assertEqual(listing.status_code,200)
            self.assertEqual(listing.get_json()['data']['orders'][0]['status'],expected)
            export=self.fx.api.get('/api/v1/exports/orders.csv',headers=fixture[3]);self.assertEqual(export.status_code,200)
            self.assertEqual(list(csv.DictReader(io.StringIO(export.get_data(as_text=True))))[0]['status'],expected)
            delivery=self.fx.fx.read(oid,fixture[3]);self.assertEqual(delivery['delivery_status'],'pending' if expected=='processing' else 'delivered')
            admin=self.owner.get('/api/orders');self.assertEqual(admin.status_code,200)
            row=next(o for o in admin.get_json()['orders'] if o['id']==oid)
            self.assertIn('Processing' if expected=='processing' else 'Completed',row['status_label'])
            if expected=='processing':self.assertEqual(self.a.action(oid,'deliver',delivery='CAPSTONE-RECEIPT').status_code,200)
        self.a.clean(fixture)
    def test_16p6_external_fulfillment_success_hmac_committed_HTTP_and_no_replay_call(self):
        self.fx.test_success_encrypted_real_delivery_and_committed_http()
        self.fx.test_transient_then_success_hmac_and_stable_idempotency()
        self.fx.test_replay_no_second_supplier_request_or_new_response_object()
        self.fx.test_router_entry_is_outside_order_atomic()
    def test_16p6_terminal_malformed_exhausted_failure_refunds_and_write_recovery(self):
        self.fx.test_terminal_failure_full_refund();self.fx.test_malformed_200_refunds()
        self.fx.test_exhausted_retry_limit_refunds_once();self.fx.test_delivery_persistence_failure_is_pending_recoverable()
    def test_16p6_config_validation_privacy_code_stock_and_manual_unchanged(self):
        self.fx.test_invalid_configuration_rejected_before_product_write()
        self.fx.test_saved_config_encrypted_write_only_blank_preserves()
        self.a.test_preserve_code_stock_manual_and_replay()
        self.a.test_supplier_adversarial_log_and_raw_exception_privacy()
    @contextlib.contextmanager
    def supplier_case(self):
        server=f.Phase16FulfillmentWiringTests.server;original=server.RequestHandlerClass
        server.RequestHandlerClass=supplier.SupplierStub
        case=supplier.Phase16SupplierFulfillmentTests('test_real_delivery_endpoint_and_exactly_one_completion');case.setUp()
        try:yield case
        finally:
            try:case.doCleanups()
            finally:server.RequestHandlerClass=original
    def test_16p7_supplier_reference_poll_completion_real_GET_and_replay(self):
        with self.supplier_case() as case:
            case.test_placement_reference_encrypted_and_replay()
            case.test_real_delivery_endpoint_and_exactly_one_completion()
    def test_16p7_failure_age_backoff_and_refund_caps(self):
        with self.supplier_case() as case:
            case.test_failure_refund_stock_and_notification();case.test_maximum_age_cutoff_without_supplier_poll()
            case.test_transient_poll_then_success_durable_backoff();case.test_partial_admin_refund_caps_automatic_failure()
    def test_16p7_crash_resume_repeated_concurrent_and_terminal_guards(self):
        with self.supplier_case() as case:
            case.test_crash_after_poll_before_persistence_then_resume()
            case.test_concurrent_workers_complete_exactly_once();case.test_concurrent_workers_refund_exactly_once()
            case.test_administrator_failure_is_skipped();case.test_stale_claim_can_be_recovered()
            case.test_failure_intent_repairs_crash_after_status()
    def test_16p8_m365_canva_pending_to_encrypted_delivery_existing_lane(self):
        self.e.test_m365_pending_then_real_endpoint_completion();self.e.test_canva_pending_then_completion()
    def test_16p8_missing_invalid_forbidden_email_and_quotes_price_no_side_effects(self):
        self.e.test_missing_email_422_both_products();self.e.test_email_validation_matrix_and_no_email_in_errors()
        self.e.test_normal_product_rejects_supplied_email_even_null();self.e.test_quotes_require_email_flag_and_no_side_effects()
    def test_16p8_private_replay_and_busy_age_failure_refund_exactly_once(self):
        self.e.test_replay_different_email_keeps_original_ciphertext_and_one_queue_row()
        self.e.test_lane_busy_parks_without_stacked_browser_thread_then_retries()
        self.e.test_permanently_busy_lane_still_age_fails_refunds();self.e.test_permanent_failure_refund_exactly_once()
    def test_16p8_provider_receipt_crash_resume_no_second_invite(self):
        self.e.test_sent_receipt_resumes_local_outbox_failure_without_second_invite()
    def test_16p9_security_findings_remain_fixed_and_auth_IDOR(self):
        self.a.test_authorization_matrix_lifecycle_and_secret_config()
        self.a.test_real_api_idor_foreign_order_indistinguishable_from_missing()
        self.a.test_owner_fail_reason_cannot_export_private_buyer_email()
        self.a.test_owner_cancel_reason_cannot_export_private_buyer_email()
        self.a.test_legacy_replacement_corrupt_ciphertext_never_reports_delivered()
        self.a.test_whitespace_delivery_roundtrips_through_actual_endpoint()
        self.a.test_separator_only_delivery_is_not_silently_discarded()
    def test_cumulative_webhook_dedup_payload_allowlist_and_privacy(self):
        self.a.test_lifecycle_event_allowlists_and_no_delivery_content()
        self.e.test_redaction_scan_real_responses_events_exports_analytics_logs()
    def test_cumulative_reconciliation_zero_after_complete_mixed_workflow(self):
        self.a.test_audit_full_mixed_workflow_zero_reconciliation_discrepancies()
    def run_prior(self,module):
        env=os.environ.copy()
        for key in list(env):
            if any(s in key for s in ('BOT_TOKEN','DATABASE_URL','BINANCE_API','ENCRYPTION_KEY','WEB_ADMIN','ADMIN_USERNAME','OWNER_USERNAME')):env.pop(key,None)
        env.update(DATABASE_URL='',BOT_TOKEN='',ADMIN_ID='0',DATA_ENCRYPTION_KEY='capstone-synthetic-only-not-production')
        result=subprocess.run([sys.executable,'-m','unittest','tests.'+module,'-q'],cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=150)
        summary=re.search(r'Ran (\d+) tests? in.*?(?:\n|$)',result.stdout)
        self.assertEqual(result.returncode,0,result.stdout[-7000:]);self.assertIsNotNone(summary,result.stdout[-2000:])
        self.assertGreater(int(summary.group(1)),0)
        # Print actual nested outcomes; do not fabricate zero-skip results.
        outcome=re.findall(r'^OK[^\n]*$',result.stdout,re.M)
        print(f'CAPSTONE_SUBSUITE {module}: cases={summary.group(1)} exit={result.returncode} outcome={outcome}',flush=True)
    def test_cumulative_unchanged_phase12_customer_purchase_and_replay_guard(self):self.run_prior('test_phase12_final_regression')
    def test_cumulative_phase15_financial_integrity_and_four_way_capstone(self):
        self.run_prior('test_phase15_final');self.run_prior('test_phase15_financial_security_audit')
    def test_cumulative_phase14_analytics_equality(self):self.run_prior('test_phase14_analytics_audit')
    def test_cumulative_real_webhook_delivery_and_earlier_capstone(self):self.run_prior('test_phase11_webhooks')
    def test_documented_and_implemented_operations_match_both_directions(self):
        public=yaml.safe_load((ROOT/'docs/openapi.yaml').read_text())
        internal=yaml.safe_load((ROOT/'docs/openapi-webadmin.yaml').read_text())
        import web_admin.app as wa
        live_public={(m,normalized(rule.rule.replace('/api/v1','',1))) for rule in self.fx.api.application.url_map.iter_rules()
                     if rule.endpoint.startswith('reseller_api_v1.') for m in rule.methods-{'HEAD','OPTIONS'}}
        live_admin={(m,normalized(rule.rule)) for rule in wa.app.url_map.iter_rules()
                    if rule.endpoint in ADMIN_ENDPOINTS for m in rule.methods-{'HEAD','OPTIONS'}}
        documented_public=operations(public);documented_admin=operations(internal)
        self.assertEqual(documented_public-live_public,set(),'documented public operation missing from app')
        self.assertEqual(live_public-documented_public,set(),'implemented public operation missing from OpenAPI')
        self.assertEqual(documented_admin-live_admin,set(),'documented internal operation missing from WebAdmin')
        self.assertEqual(live_admin-documented_admin,set(),'Phase 16 internal operation missing from OpenAPI')
        self.assertEqual({rule.endpoint for rule in wa.app.url_map.iter_rules() if rule.endpoint in ADMIN_ENDPOINTS},ADMIN_ENDPOINTS)
        self.assertIn('openapi-webadmin.yaml',public['externalDocs']['url'])
    def test_documentation_contracts_guide_audit_and_quote_compatibility(self):
        spec=yaml.safe_load((ROOT/'docs/openapi.yaml').read_text())
        self.assertNotIn('status',spec['components']['schemas']['Quote']['properties'])
        self.assertIn('requires_buyer_email',spec['components']['schemas']['Quote']['properties'])
        self.assertIn('processing',spec['components']['schemas']['OrderSummary']['properties']['status']['enum'])
        guide=(ROOT/'RESELLER_API.md').read_text()
        for term in ('buyer_email','requires_buyer_email','fulfillment','status=processing','openapi-webadmin.yaml'):
            self.assertIn(term,guide)
        report=(ROOT/'PHASE16_OPERATIONS_AUDIT.md').read_text()
        for term in ('F16-01','F16-02','F16-03','F16-04','Reconciliation','unverified'):
            self.assertIn(term,report)

if __name__=='__main__':unittest.main()
