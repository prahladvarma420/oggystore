#!/usr/bin/env python3
"""Manual TEST Railway/PostgreSQL validation, NEVER a unittest load run.
Use --execute after reviewing preflight. No redirects, no global limit changes,
no automatic HTTP retries, no supplier calls, no raw delivery/log output.
"""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import re
import sys
import threading
import time
from staging_load_common import (application_environment, database_fingerprint,
    guarded_database, identifier, private_config, run_prefix, safe_main, write_report, validate_report_path, SafetyError)
from verify_staging_load import assess, snapshot

ENDPOINTS = (
    ('products', 'GET', '/products'), ('create_order', 'POST', '/orders'),
    ('orders', 'GET', '/orders'), ('delivery', 'GET', '/orders/{order_id}/delivery'),
    ('sales_analytics', 'GET', '/analytics/sales'),
    ('orders_csv', 'GET', '/exports/orders.csv'),
    ('transactions', 'GET', '/transactions'), ('invoices', 'GET', '/invoices'))
# Source: tests/test_phase14_analytics_audit.py. These are single-request
# fixture assertions, NOT a newly invented p95/p99 staging SLA.
REFERENCE_BUDGET_MS = {'sales_analytics': 1500}
OWNER_MONITORING_REFERENCE_MS = 2500  # owner endpoints not requested/measured here

def percentile(values, quantile):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lo, hi = math.floor(position), math.ceil(position)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo), 3)

def summary(rows, budget=None):
    values = [r['elapsed_ms'] for r in rows]
    successful = [r['elapsed_ms'] for r in rows if r['ok']]
    return {'requests': len(rows), 'successful': len(successful),
            'failed': len(rows) - len(successful),
            'http_status_counts': dict(Counter(str(r['http_status']) for r in rows)),
            'all_attempts_ms': {f'p{q}': percentile(values, q / 100) for q in (50, 95, 99)},
            'successful_ms': {f'p{q}': percentile(successful, q / 100) for q in (50, 95, 99)},
            'reference_budget_ms': budget,
            'reference_comparison': ('NOT_APPLICABLE_NO_DEFINED_BUDGET' if budget is None else
                'NOT_MEASURED' if not rows else 'MISSED_OR_ERRORS' if
                any(not r['ok'] or r['elapsed_ms'] >= budget for r in rows) else 'MET_FOR_OBSERVED_SAMPLES'),
            'note': 'Percentiles are interpolated observations, not a new percentile SLA.'}

def http_request(cfg, method, path, payload=None, timeout=10, barrier=None, expected_product=None):
    import requests
    if barrier:
        barrier.wait(timeout=60)
    start = time.perf_counter()
    result = {'http_status': 0, 'ok': False, 'category': 'transport_error',
              'order_id': None, 'expected_stock_rejection': False}
    try:
        # Per-call sessions avoid shared mutable state across 200 threads.
        with requests.Session() as session:
            session.trust_env = False  # no implicit proxy/netrc credential changes
            with session.request(method, cfg['base_url'].rstrip('/') + path,
                    headers={'Authorization': 'Bearer ' + cfg['api_token']},
                    json=payload, timeout=(min(5, timeout), timeout),
                    allow_redirects=False, stream=True, verify=True) as response:
                result['http_status'] = response.status_code
                body = bytearray()
                csv = path.startswith('/exports/')
                for chunk in response.iter_content(65536):
                    if time.perf_counter() - start > timeout:
                        raise TimeoutError
                    if not csv:
                        body.extend(chunk)
                        if len(body) > 4 * 1024 * 1024:
                            raise ValueError
                if csv:
                    result['ok'] = response.status_code == 200 and 'csv' in response.headers.get('Content-Type', '').lower()
                else:
                    decoded = json.loads(body)
                    result['ok'] = response.status_code in (200, 201) and decoded.get('ok') is True
                    data = decoded.get('data') or {}
                    if method == 'POST' and result['ok']:
                        oid = data.get('order_id', '')
                        result['ok'] = bool(re.fullmatch(r'ORD-\d{8,}', oid)) and data.get('client_order_id') == payload['client_order_id']
                        if result['ok']:
                            result['order_id'] = oid
                    if expected_product:
                        result['ok'] = result['ok'] and data.get('product_id') == expected_product
                    if path.endswith('/delivery'):
                        delivery = data.get('delivery') or {}
                        result['ok'] = result['ok'] and delivery.get('delivery_status') == 'delivered' and bool(delivery.get('items'))
                    error = decoded.get('error') or {}
                    result['expected_stock_rejection'] = (response.status_code == 422 and
                        error.get('code') == 'insufficient_stock')
                result['category'] = 'success' if result['ok'] else 'stock_exhausted' if result['expected_stock_rejection'] else 'http_or_contract_error'
    except Exception:
        # No URLs, token, exception text, response bodies or delivery saved.
        result['category'] = 'transport_or_invalid_response'
    result['elapsed_ms'] = round((time.perf_counter() - start) * 1000, 3)
    return result

def burst_payloads(product, run_id):
    # Exactly 200 requests / 100 logical orders, duplicates start concurrently.
    prefix = run_prefix(run_id) + 'burst.'
    return [{'product_id': product, 'quantity': 1, 'client_order_id': prefix + str(i // 2)} for i in range(200)]

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True)
    p.add_argument('--seed-report', required=True)
    p.add_argument('--run-id', required=True, type=identifier)
    p.add_argument('--report', required=True)
    p.add_argument('--requests', type=int, default=20, help='per endpoint, 1..1000')
    p.add_argument('--concurrency', type=int, default=4, help='ordinary measurements, 1..32; burst always 200')
    p.add_argument('--timeout', type=float, default=10, help='per-call read and observed total budget, 1..60 seconds')
    p.add_argument('--drain-timeout', type=int, default=300, help='1..3600 seconds')
    p.add_argument('--execute', action='store_true')
    a = p.parse_args()
    if not (1 <= a.requests <= 1000 and 1 <= a.concurrency <= 32 and
            1 <= a.timeout <= 60 and 1 <= a.drain_timeout <= 3600):
        raise SafetyError('load_parameters_outside_bounds')
    from pathlib import Path
    validate_report_path(a.report)
    if Path(a.report).exists():
        raise SafetyError('report_already_exists_use_verifier_not_blind_rerun')
    cfg = private_config(a.config)
    seed = json.loads(Path(a.seed_report).read_text(encoding='utf-8'))
    if seed.get('status') != 'SEEDED' or seed.get('database_fingerprint') != database_fingerprint(cfg):
        raise SafetyError('verified_seed_report_required')
    if seed['actual_orders'] < 50000 or seed['actual_stock'] < 100000 or seed['actual_resellers'] < 50:
        raise SafetyError('minimum_dataset_not_met')
    # 100 logical orders in duplicate pairs must exceed available stock.
    if not 1 <= seed['limited_stock'] < 100:
        raise SafetyError('burst_requires_fewer_than_100_available_units')
    with guarded_database(cfg) as conn:
        application_environment(cfg)
        from reseller_api.services.authentication import authenticate_credentials
        from reseller_api.services.webhook_delivery import get_active_subscriptions
        from reseller_api.services.reconciliation import reconcile_reseller
        token = cfg.get('api_token', '')
        if '.' not in token:
            raise SafetyError('test_credentials_required')
        principal = authenticate_credentials(*token.split('.', 1), 'test')
        if not principal or principal.reseller_public_id != seed['load_reseller_public_id']:
            raise SafetyError('key_not_active_dedicated_test_reseller')
        rid = principal.reseller_id
        lock = conn.execute('SELECT pg_try_advisory_lock(hashtext(%s)) AS ok', ('oggy-load-account-' + str(rid),)).fetchone()['ok']
        if not lock:
            raise SafetyError('dedicated_load_account_already_running')
        if reconcile_reseller(rid):
            raise SafetyError('preexisting_reconciliation_discrepancies')
        details = {}
        for kind, public in seed['products'].items():
            row = conn.execute('SELECT id,name,price,active,fulfillment_type FROM products WHERE public_id=%s', (public,)).fetchone()
            if (not row or row['name'] != 'LOADTEST:' + seed['namespace'] + ':' + kind or
                    row['active'] != 1 or row['price'] != 1 or row['fulfillment_type'] not in (None, 'code_stock')):
                raise SafetyError('dedicated_seed_product_changed')
            details[kind] = row['id']
        actual_orders = conn.execute("SELECT COUNT(*) AS n FROM orders WHERE source='reseller_api' AND product_id=%s", (details['history'],)).fetchone()['n']
        actual_stock = conn.execute('SELECT COUNT(*) AS n FROM product_stock WHERE product_id=%s', (details['history'],)).fetchone()['n']
        actual_resellers = conn.execute('SELECT COUNT(*) AS n FROM resellers WHERE name LIKE %s', ('LOADTEST:' + seed['namespace'] + ':reseller:%',)).fetchone()['n']
        if actual_orders != seed['actual_orders'] or actual_stock != seed['actual_stock'] or actual_resellers != seed['actual_resellers']:
            raise SafetyError('seed_report_no_longer_matches_database')
        # Authenticated READ canary against operator-confirmed TEST host.
        # Cannot alone prove deployment isolation; explicit operator confirmation
        # is mandatory, and newly created order/DB correlation is checked below.
        canary = http_request(cfg, 'GET', '/my-products/' + seed['products']['limited'], timeout=a.timeout, expected_product=seed['products']['limited'])
        if not canary['ok']:
            raise SafetyError('authenticated_test_catalog_canary_failed')
        subscriptions = [get_active_subscriptions(rid, event) for event in ('order.created', 'delivery.completed')]
        if any(len(rows) != 1 for rows in subscriptions) or subscriptions[0][0]['id'] != subscriptions[1][0]['id']:
            raise SafetyError('exactly_one_shared_test_receiver_required_for_both_events')
        webhook_id = subscriptions[0][0]['id']
        prefix = run_prefix(a.run_id) + 'burst.'
        before = snapshot(conn, rid, details['limited'], prefix, webhook_id)
        if before['orders'] or before['stock']['sold'] or before['stock']['available'] != seed['limited_stock']:
            raise SafetyError('limited_product_not_pristine_use_new_seed_namespace')
        if before['wallet'] < (a.requests + seed['limited_stock']) * 100:
            raise SafetyError('insufficient_test_wallet_budget')
        from database.reseller_db import get_reseller_limits
        limits = get_reseller_limits(rid)
        planned = 8 * a.requests + 401
        if not limits or any(limits[k] < planned for k in ('rate_limit_per_min', 'daily_request_limit', 'monthly_request_limit')) or any(limits[k] < 201 + a.requests for k in ('orders_per_min', 'daily_order_limit', 'monthly_order_limit')):
            raise SafetyError('raise_only_dedicated_account_limits_via_existing_webadmin_editor')
        report = {'status': 'PREFLIGHT_ONLY', 'environment': 'test', 'run_id': a.run_id,
                  'database_fingerprint': database_fingerprint(cfg),
                  'reseller_id': rid, 'webhook_id': webhook_id, 'limited_product_id': details['limited'],
                  'dataset': {k: seed[k] for k in ('actual_orders', 'actual_stock', 'actual_resellers')},
                  'ordinary_concurrency': a.concurrency, 'burst_concurrency': 200,
                  'burst_logical_orders': 100, 'request_timeout_seconds': a.timeout,
                  'started_at': datetime.now(timezone.utc).isoformat(),
                  'owner_monitoring': {'reference_budget_ms': OWNER_MONITORING_REFERENCE_MS, 'status': 'NOT_MEASURED_NOT_IN_ENDPOINT_SET'},
                  'endpoint_results': {}, 'burst_responses': [], 'replays': []}
        if not a.execute:
            write_report(a.report, report)
            print('PREFLIGHT_ONLY: no orders created; manual --execute required.')
            return
        # Record intent BEFORE any write HTTP. Refuse overwrite on any rerun.
        report['status'] = 'RUNNING'
        write_report(a.report, report)
        perf = seed['products']['performance']
        delivery_id = None
        for name, method, template in ENDPOINTS:
            if name == 'delivery' and not delivery_id:
                report['endpoint_results'][name] = {'status': 'NOT_RUN_NO_VERIFIED_ORDER'}
                continue
            path = template.format(order_id=delivery_id)
            def request(i):
                body = {'product_id': perf, 'quantity': 1, 'client_order_id': run_prefix(a.run_id) + 'bench.' + str(i)} if method == 'POST' else None
                return http_request(cfg, method, path, body, a.timeout)
            with ThreadPoolExecutor(max_workers=a.concurrency) as pool:
                rows = list(pool.map(request, range(a.requests)))
            report['endpoint_results'][name] = summary(rows, REFERENCE_BUDGET_MS.get(name))
            if method == 'POST':
                winners = [r['order_id'] for r in rows if r['ok']]
                if winners:
                    delivery_id = winners[0]
                    dbrow = conn.execute("SELECT id FROM orders WHERE reseller_id=%s AND client_order_id=%s AND product_id=%s", (rid, run_prefix(a.run_id) + 'bench.0', details['performance'])).fetchone()
                    # Correlate each HTTP acknowledgment to THIS PostgreSQL DB.
                    expected = {'ORD-' + str(r['id']).zfill(8) for r in conn.execute("SELECT id FROM orders WHERE reseller_id=%s AND client_order_id LIKE %s AND product_id=%s", (rid, run_prefix(a.run_id) + 'bench.%', details['performance'])).fetchall()}
                    if set(winners) != expected or not dbrow:
                        report['status'] = 'FAILED_TARGET_DATABASE_CORRELATION'
                        write_report(a.report, report)
                        return 1
            write_report(a.report, report)
        if not delivery_id:
            report['status'] = 'FAILED_NO_DATABASE_CORRELATED_ORDER'
            write_report(a.report, report)
            return 1
        before = snapshot(conn, rid, details['limited'], prefix, webhook_id)
        report['burst_before'] = before
        payloads = burst_payloads(seed['products']['limited'], a.run_id)
        report['burst_client_order_ids'] = [p['client_order_id'] for p in payloads]
        write_report(a.report, report)
        barrier = threading.Barrier(200)
        def fire(payload):
            result = http_request(cfg, 'POST', '/orders', payload, a.timeout, barrier)
            result['client_order_id'] = payload['client_order_id']
            return result
        with ThreadPoolExecutor(max_workers=200) as pool:
            report['burst_responses'] = list(pool.map(fire, payloads))
        write_report(a.report, report)
        # Retry every committed logical order, not new keys; assert same ORD ID.
        winners = {r['client_order_id']: r['order_id'] for r in report['burst_responses'] if r['ok']}
        for client, oid in winners.items():
            result = http_request(cfg, 'POST', '/orders', {'product_id': seed['products']['limited'], 'quantity': 1, 'client_order_id': client}, a.timeout)
            report['replays'].append({'same_order': result['ok'] and result['http_status'] == 200 and result['order_id'] == oid, 'http_status': result['http_status']})
        deadline = time.monotonic() + a.drain_timeout
        while True:
            after = snapshot(conn, rid, details['limited'], prefix, webhook_id)
            assessment = assess(before, after, report['burst_responses'])
            if assessment['checks']['worker_http_delivery_evidence'] or time.monotonic() >= deadline:
                break
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        report['burst_after'] = after
        report['burst_assessment'] = assessment
        report['reconciliation_discrepancies'] = len(reconcile_reseller(rid))
        measured_ok = all(r.get('requests') == a.requests and r.get('failed') == 0 for r in report['endpoint_results'].values())
        outcomes_ok = all(r['ok'] or r['expected_stock_rejection'] for r in report['burst_responses'])
        replays_ok = bool(report['replays']) and all(r['same_order'] for r in report['replays'])
        report['status'] = 'COMPLETE' if (measured_ok and outcomes_ok and replays_ok and assessment['verified'] and not report['reconciliation_discrepancies']) else 'FAILED'
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        report['performance_exceptions'] = [name for name, r in report['endpoint_results'].items() if r.get('reference_comparison') == 'MISSED_OR_ERRORS']
        report['note'] = 'COMPLETE means measured invariants passed; review performance exceptions and receiver durable processing separately.'
        write_report(a.report, report)
        print(report['status'] + ': sanitized evidence written; see reference comparisons and failed checks.')
        return 0 if report['status'] == 'COMPLETE' else 1
if __name__ == '__main__':
    sys.exit(safe_main(main))
