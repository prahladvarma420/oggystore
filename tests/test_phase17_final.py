"""Prompt17.10 local integration/cumulative gate; NOT a live-launch rehearsal.

Existing real routes/services/temp SQLite assertions execute in separate child
interpreters to retain import-time database isolation. Chain/supplier/Telegram
providers are test substitutes; Flask/Express reference receivers use real local
HTTP. Nested counts are printed separately, never added to runner totals.
No production credentials/data, external load or deployment writes. Passing this
suite cannot close missing hosted CI, PostgreSQL/staging or real sandbox gates.
"""
from __future__ import annotations
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
CHILD='''import json,sys,unittest
suite=unittest.defaultTestLoader.loadTestsFromNames(json.loads(sys.argv[1]))
r=unittest.TextTestRunner(verbosity=2).run(suite)
counts=dict(tests=r.testsRun,failures=len(r.failures),errors=len(r.errors),skipped=len(r.skipped),unexpected_successes=len(r.unexpectedSuccesses),expected_failures=len(r.expectedFailures))
with open(sys.argv[2],'w') as f:json.dump(counts,f)
sys.exit(0 if r.wasSuccessful() else 1)
'''
DEPOSIT_METHODS=('valid_all_rails_and_reference','repeat_is_exactly_once_and_one_event',
 'concurrent_same_deposit','transaction_cannot_credit_two_resellers',
 'customer_then_reseller_collision','reseller_then_customer_collision',
 'cross_table_concurrent_claim_one_winner','atomic_security_failure_and_resume',
 'ledger_exception_rolls_back_claim','expired_no_automatic_late_credit',
 'exact_chain_amount_tolerance_and_no_partial_aggregation',
 'binance_overpayment_capped_and_underpayment_rejected','suspended_receives_eligible_credit',
 'wallet_surfaces_and_balance_chain','wrong_network_rpc_is_rejected',
 'bep_actual_checker_credits_reseller_from_verified_logs','trc_checker_real_boundary_and_notifications_after_commit')

class Phase17FinalTests(unittest.TestCase):
    def run_names(self,names):
        from tools.run_tests import test_environment
        with tempfile.TemporaryDirectory(prefix='phase17-final-counts-') as d:
            p=Path(d)/'result.json'
            env=test_environment()
            # The outer runner excludes node_modules from its disposable copy,
            # but supplies a read-only dependency path for Node round trips.
            # Retain that dependency capability for nested children, not arbitrary
            # application credentials or NODE_OPTIONS.
            if 'NODE_PATH' not in env and os.environ.get('NODE_PATH'):
                deps=Path(os.environ['NODE_PATH'])
                if deps.is_absolute() and deps.name=='node_modules' and (deps/'express/package.json').is_file():
                    env['NODE_PATH']=str(deps)
            result=subprocess.run([sys.executable,'-c',CHILD,json.dumps(names),str(p)],
                cwd=ROOT,env=env,capture_output=True,text=True,timeout=180)
            diagnostic=(result.stdout+'\n'+result.stderr)[-16000:]
            self.assertEqual(result.returncode,0,diagnostic)
            self.assertTrue(p.exists(),'No child result channel: '+diagnostic)
            counts=json.loads(p.read_text())
            self.assertGreater(counts['tests'],0)
            for k in ('failures','errors','skipped','unexpected_successes','expected_failures'):
                self.assertEqual(counts[k],0,f'{k}: '+diagnostic)
            print('FINAL_GATE_EVIDENCE '+json.dumps({'names':names,**counts},sort_keys=True),flush=True)
    def module(self,name):self.run_names(['tests.'+name])
    def test_17p1_rails_verification_replay_caps_collision_and_ledger_surfaces(self):
        self.run_names(['tests.test_phase17_reseller_deposits.ResellerDepositTests.test_'+m for m in DEPOSIT_METHODS])
    def test_17p2_authorization_topup_restart_cancel_notify_and_customer_guards(self):self.module('test_phase17_topup_flow')
    def test_17p3_products_prices_api_parity_large_catalog_and_fresh_gates(self):self.module('test_phase17_telegram_catalog')
    def test_17p4_statistics_docs_route_accuracy_privacy_and_removed_stubs(self):self.module('test_phase17_telegram_stats_docs')
    def test_17p5_status_and_order_notifications_failure_nonblocking_secret_hygiene(self):self.module('test_phase17_notifications')
    def test_17p6_runner_isolation_timeout_crash_filters_counts_and_outside_cwd(self):self.module('test_phase17_test_runner')
    def test_17p7_both_real_reference_receivers_signer_roundtrip_dedupe_and_catalog(self):self.module('test_phase17_client_kit')
    def test_17p8_load_tool_guards_not_live_load_proof(self):self.module('test_phase17_load_static')
    def test_17p9_security_delta_backups_and_unchanged_four_prior_audits(self):self.module('test_phase17_security_delta')
    def test_cumulative_customer_purchase_replay_guard(self):self.module('test_phase12_final_regression')
    def test_cumulative_parallel_order_idempotency_and_wallet(self):self.module('test_phase12_concurrency_idempotency')
    def test_cumulative_webhooks_real_service_delivery_and_dedupe(self):self.module('test_phase11_webhooks')
    def test_cumulative_financial_equality_statements_invoices_and_reconciliation(self):self.module('test_phase15_final')
    def test_cumulative_phase16_lifecycle_status_fulfillment_and_bidirectional_docs(self):self.module('test_phase16_final')
    def test_ci_workflow_runs_documented_runner_and_keeps_diagnostics(self):
        import yaml
        workflow=yaml.safe_load((ROOT/'.github/workflows/tests.yml').read_text())
        self.assertEqual(set(workflow.get('on',workflow.get(True))),{'push','pull_request'})
        job=workflow['jobs']['tests'];steps=job['steps'];commands=[s.get('run','') for s in steps]
        for command in ('python tools/run_tests.py','python -m compileall .','python verify_patch.py','python tools/scan_secrets.py','python tools/generate_client_kit.py --check'):
            self.assertIn(command,commands)
        self.assertTrue(any('pip install -r requirements.txt pyyaml' in c for c in commands))
        self.assertTrue(any('npm ci --prefix examples --ignore-scripts' in c for c in commands))
        self.assertTrue(any(s.get('uses','').startswith('actions/upload-artifact@') and s.get('if')=='always()' and s.get('with',{}).get('path')=='.test-results/' for s in steps))
        self.assertIsNone(re.search(r'\$\{\{\s*secrets(?:\.|\[)',(ROOT/'.github/workflows/tests.yml').read_text()))
        # Workflow shape is verified here; no claim that hosted CI was run.
    def test_master_plan_priority_inventory_preserved_and_gated(self):
        plan=(ROOT/'RESELLER_API_FUTURE_PROMPTS.md').read_text()
        gate=json.loads((ROOT/'PHASE17_READINESS_GATE.json').read_text())
        self.assertEqual(hashlib.sha256(plan.encode()).hexdigest(),gate['master_plan_sha256'])
        sections=dict(re.findall(r'^## (P[01])[^\n]*\n(.*?)(?=^## |\Z)',plan,re.M|re.S))
        entries=[(priority,line[6:]) for priority,body in sections.items() for line in body.splitlines() if line.startswith('- [ ] ')]
        self.assertEqual(len(entries),12)
        self.assertEqual([(x['priority'],x['source_text']) for x in gate['backlog']],entries)
        for entry in gate['backlog']:
            self.assertIn(entry['status'],{'LOCAL_VERIFIED','BLOCKED','NOT_TESTED','FAILED','VERIFIED'})
            if entry['status']=='LOCAL_VERIFIED':self.assertTrue(entry['required_suites'])
        if any(x['status']!='VERIFIED' for x in gate['backlog']):
            self.assertEqual(gate['project_status'],'BLOCKED')
    def test_live_readiness_requires_real_evidence_not_static_file_presence(self):
        gate=json.loads((ROOT/'PHASE17_READINESS_GATE.json').read_text())
        text=(ROOT/'GO_LIVE.md').read_text()
        self.assertEqual(gate['schema_version'],1)
        ids=set()
        for item in gate['checks']:
            self.assertNotIn(item['id'],ids);ids.add(item['id'])
            self.assertIn(item['status'],{'VERIFIED','NOT_TESTED','FAILED','BLOCKED','NOT_APPLICABLE'})
            self.assertTrue(item['reason']);self.assertIn('`'+item['id']+'`',text)
            if item['status']=='VERIFIED':
                self.assertTrue(item['evidence'])
                self.assertIn('- [x] `'+item['id']+'`',text)
                for evidence in item['evidence']:
                    path=Path(evidence['path'])
                    self.assertFalse(path.is_absolute());self.assertNotIn('..',path.parts)
                    self.assertTrue((ROOT/path).is_file())
                if item['scope']=='live':
                    # A local unit-test summary is never evidence for real rehearsal.
                    self.assertTrue(item.get('operator_attestation'))
                    self.assertTrue(item.get('environment_binding'))
                    self.assertTrue(item.get('performed_at'))
                    self.assertNotIn('mock',item['operator_attestation'].lower())
                elif item['required_suites']:
                    report=json.loads((ROOT/'PHASE17_FINAL_TEST_RESULTS.json').read_text())
                    self.assertTrue(report['full_regression']['complete'])
                    self.assertEqual(report['full_regression']['exit_code'],0)
                    rows={r['suite']:r for r in report['full_regression']['results']}
                    for suite in item['required_suites']:
                        self.assertIn(suite,rows);r=rows[suite]
                        self.assertEqual(r['exit_code'],0);self.assertGreater(r['passed'],0)
                        self.assertEqual(r.get('failures',0)+r.get('errors',0),0)
            else:self.assertIn('- [ ] `'+item['id']+'`',text)
        for identifier in ('hosted_ci','staging_load','staging_concurrency','webhook_burst','kill_switch_rehearsal','refund_sop_rehearsal','pg_restore_rehearsal','sandbox_reseller_a','sandbox_reseller_b','production_configuration','environment_markers','rollback_rehearsal','master_p0_p1_closed'):
            self.assertIn(identifier,ids)
        live=[x for x in gate['checks'] if x['scope']=='live' and x['status'] not in {'VERIFIED','NOT_APPLICABLE'}]
        open_backlog=[x for x in gate['backlog'] if x['status']!='VERIFIED']
        if live or open_backlog:
            self.assertEqual(gate['project_status'],'BLOCKED');self.assertFalse(gate['go_live_authorized'])
            self.assertIn('NOT READY FOR GO-LIVE',text)
    def test_current_deployment_docs_cover_all_workers_and_safe_rollback_boundary(self):
        docs=(ROOT/'RAILWAY_RESELLER_API.md').read_text()
        proc=(ROOT/'Procfile').read_text()
        for name in ('webhook','fulfillment'):
            command=f'python -m reseller_api.workers.{name}_worker'
            self.assertIn(command,docs);self.assertIn(f'{name}-worker: {command}',proc)
        self.assertIn('not a database restore',docs)
        self.assertIn('optional Telegram notification',docs)
        versioning=(ROOT/'RESELLER_API_VERSIONING.md').read_text()
        self.assertIn('timestamp',versioning)
        self.assertIn('raw body',versioning)
        self.assertIn('300',versioning)

if __name__=='__main__':unittest.main()
