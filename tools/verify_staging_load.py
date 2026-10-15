#!/usr/bin/env python3
"""Read-only burst evidence checks, used by load_test and manual re-verification.
A delivered outbox flag without a successful HTTP delivery log NEVER passes.
"""
from __future__ import annotations
import argparse
import json
import sys
from staging_load_common import (guarded_database, private_config, run_prefix,
                                safe_main, write_report, SafetyError)

def snapshot(conn, rid, pid, prefix, webhook_id=None):
    # One consistent snapshot; readonly transaction, no data changes.
    with conn.transaction():
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        wallet = conn.execute('SELECT balance_cents FROM reseller_wallet WHERE reseller_id=%s', (rid,)).fetchone()
        totals = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents),0) AS cents FROM reseller_wallet_transactions WHERE reseller_id=%s AND tx_type='purchase_debit'", (rid,)).fetchone()
        stock = conn.execute('SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE sold=0) AS available, COUNT(*) FILTER (WHERE sold=1) AS sold FROM product_stock WHERE product_id=%s', (pid,)).fetchone()
        orders = conn.execute('SELECT id,reseller_id,request_id,client_order_id,quantity,unit_price_cents FROM orders WHERE reseller_id=%s AND product_id=%s AND client_order_id LIKE %s ORDER BY id', (rid, pid, prefix + '%')).fetchall()
        claims = conn.execute('SELECT order_id,COUNT(*) AS n FROM product_stock WHERE product_id=%s AND sold=1 GROUP BY order_id', (pid,)).fetchall()
        events = conn.execute("SELECT o.id,o.event_type,o.aggregate_id,o.status,EXISTS(SELECT 1 FROM webhook_delivery_log l WHERE l.outbox_event_id=o.id AND l.success=1 AND l.http_status BETWEEN 200 AND 299 AND l.webhook_id=COALESCE(%s,l.webhook_id)) AS http_delivered FROM webhook_outbox o WHERE o.reseller_id=%s AND o.aggregate_id IN (SELECT 'ORD-' || LPAD(id::text,GREATEST(8,LENGTH(id::text)),'0') FROM orders WHERE reseller_id=%s AND product_id=%s AND client_order_id LIKE %s) ORDER BY o.id", (webhook_id, rid, rid, pid, prefix + '%')).fetchall()
        debits = conn.execute("SELECT t.reference,t.amount_cents FROM reseller_wallet_transactions t JOIN orders o ON t.reference=('rapi.' || o.reseller_id::text || '.' || o.request_id) WHERE t.reseller_id=%s AND t.tx_type='purchase_debit' AND o.product_id=%s AND o.client_order_id LIKE %s", (rid, pid, prefix + '%')).fetchall()
        return {'wallet': wallet['balance_cents'] if wallet else 0,
                'ledger_count': totals['n'], 'ledger_cents': totals['cents'],
                'stock': dict(stock), 'orders': [dict(r) for r in orders],
                'claims': [dict(r) for r in claims], 'events': [dict(r) for r in events],
                'debits': [dict(r) for r in debits]}

def assess(before, after, responses):
    orders = after['orders']
    ids = {r['id'] for r in orders}
    unique_clients = {r['client_order_id'] for r in orders}
    expected_charge = sum(r['quantity'] * r['unit_price_cents'] for r in orders)
    quantity = sum(r['quantity'] for r in orders)
    claims = {r['order_id']: r['n'] for r in after['claims']}
    expected_events = {('ORD-' + str(r['id']).zfill(8), kind)
                       for r in orders for kind in ('order.created', 'delivery.completed')}
    found_events = {(r['aggregate_id'], r['event_type']) for r in after['events']}
    acknowledged = {r['order_id'] for r in responses if r.get('order_id')}
    database_public_ids = {'ORD-' + str(i).zfill(8) for i in ids}
    debit_map = {d['reference']: d['amount_cents'] for d in after['debits']}
    checks = {
        'orders_created': bool(orders),
        'limited_stock_exhausted': quantity == before['stock']['available'],
        'no_oversell': quantity <= before['stock']['available'],
        'unchanged_stock_total': before['stock']['total'] == after['stock']['total'],
        'stock_delta_matches_orders': before['stock']['available'] - after['stock']['available'] == quantity,
        'stock_attribution_matches': all(claims.get(r['id']) == r['quantity'] for r in orders) and set(claims) == ids,
        'unique_client_orders': len(unique_clients) == len(orders),
        'debit_count_matches': after['ledger_count'] - before['ledger_count'] == len(orders) == len(after['debits']),
        'debit_amounts_match': len(debit_map) == len(orders) and all(debit_map.get('rapi.' + str(r['reseller_id']) + '.' + r['request_id']) == -r['quantity'] * r['unit_price_cents'] for r in orders) and -sum(d['amount_cents'] for d in after['debits']) == expected_charge,
        'ledger_delta_matches': before['ledger_cents'] - after['ledger_cents'] == expected_charge,
        'wallet_delta_matches': before['wallet'] - after['wallet'] == expected_charge,
        'http_orders_match_database': acknowledged == database_public_ids,
        'expected_outbox_exactly_once': found_events == expected_events and len(after['events']) == len(expected_events),
        'worker_http_delivery_evidence': bool(expected_events) and all(r['status'] == 'delivered' and r['http_delivered'] for r in after['events']),
    }
    return {'checks': checks, 'verified': all(checks.values()),
            'orders': len(orders), 'quantity': quantity, 'charge_cents': expected_charge,
            'outbox_events': len(after['events'])}

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True)
    p.add_argument('--evidence', required=True)
    p.add_argument('--report', required=True)
    a = p.parse_args()
    cfg = private_config(a.config)
    data = json.loads(open(a.evidence, encoding='utf-8').read())
    if data.get('environment') != 'test' or data.get('status') not in ('COMPLETE', 'FAILED', 'RUNNING'):
        raise SafetyError('invalid_load_evidence')
    with guarded_database(cfg) as conn:
        from staging_load_common import database_fingerprint
        if data['database_fingerprint'] != database_fingerprint(cfg):
            raise SafetyError('evidence_database_mismatch')
        after = snapshot(conn, data['reseller_id'], data['limited_product_id'], run_prefix(data['run_id']) + 'burst.', data['webhook_id'])
        result = assess(data['burst_before'], after, data.get('burst_responses', []))
        from staging_load_common import application_environment
        application_environment(cfg)
        from reseller_api.services.reconciliation import reconcile_reseller
        result['reconciliation_discrepancies'] = len(reconcile_reseller(data['reseller_id']))
        result['verified'] = result['verified'] and not result['reconciliation_discrepancies']
        result['note'] = 'Read-only repeat check; does not replace performance or replay results.'
        write_report(a.report, result)
        print('VERIFIED burst invariants' if result['verified'] else 'NOT VERIFIED: review failed checks')
        return 0 if result['verified'] else 1
if __name__ == '__main__':
    sys.exit(safe_main(main))
