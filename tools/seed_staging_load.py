#!/usr/bin/env python3
"""Manual TEST-only dataset builder. Default dry-run; never imports migration.
Uses app schema/helpers and generated IDs (no copied IDs/sequence resets).
Every historical order uses validation/pricing/stock/order service + ledger.
A session advisory lock and deterministic names/client IDs make reruns resumable.
"""
from __future__ import annotations
import argparse
import math
import sys
from staging_load_common import (application_environment, database_fingerprint,
    guarded_database, identifier, private_config, safe_main, write_report, validate_report_path, SafetyError)

def seed_plan(orders=50000, stock=100000, resellers=50):
    if not 50000 <= orders <= 1000000 or not orders * 2 <= stock <= 2000000 or not 50 <= resellers <= 1000:
        raise SafetyError('seed_sizes_outside_bounds_or_insufficient_stock')
    return {'orders': orders, 'stock': stock, 'resellers': resellers,
            'orders_per_reseller_max': math.ceil(orders / resellers)}

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True)
    p.add_argument('--namespace', required=True, type=identifier)
    p.add_argument('--orders', type=int, default=50000)
    p.add_argument('--stock', type=int, default=100000)
    p.add_argument('--resellers', type=int, default=50)
    p.add_argument('--limited-stock', type=int, default=40)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--report', required=True)
    a = p.parse_args()
    validate_report_path(a.report)
    plan = seed_plan(a.orders, a.stock, a.resellers)
    if not 1 <= a.limited_stock < 100:
        raise SafetyError('limited_stock_must_be_between_1_and_99')
    cfg = private_config(a.config)
    # Read-only safety/preflight occurs even in dry-run; no schema initialization.
    with guarded_database(cfg) as guard:
        if not a.apply:
            write_report(a.report, {'status': 'DRY_RUN', 'namespace': a.namespace,
                'plan': plan, 'database_fingerprint': database_fingerprint(cfg),
                'note': 'No writes, counts not yet achieved; manual --apply required.'})
            print('DRY_RUN: no writes; planned sizes saved.')
            return
        locked = guard.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                               ('oggy-load-seed-' + a.namespace,)).fetchone()['ok']
        if not locked:
            raise SafetyError('seed_namespace_already_running')
        application_environment(cfg)
        import database.db as db
        import database.reseller_db as rdb
        from utils.crypto import encrypt_text
        from reseller_api.context import ApiRequestContext
        from reseller_api.services.order_validation import validate_order_request
        from reseller_api.services.order_pricing import calculate_order_price
        from reseller_api.services.order_stock import reserve_and_validate_stock
        from reseller_api.services.order_service import create_reseller_order, find_client_order
        db.init_db()
        cur, conn = db.get_cursor(), db.get_conn()
        stem = 'LOADTEST:' + a.namespace + ':'
        settings_key = 'loadtest.seed.' + a.namespace
        import json
        cur.execute('SELECT value FROM settings WHERE key=?', (settings_key,))
        saved = cur.fetchone()
        signature = dict(plan, limited_stock=a.limited_stock)
        if saved and json.loads(saved[0]) != signature:
            raise SafetyError('namespace_plan_changed_use_new_namespace')
        if not saved:
            cur.execute('INSERT INTO settings(key,value) VALUES(?,?)',
                        (settings_key, json.dumps(signature, sort_keys=True)))
            conn.commit()
        def product(kind, count):
            name = stem + kind
            cur.execute('SELECT id FROM products WHERE name=?', (name,))
            rows = cur.fetchall()
            if len(rows) > 1:
                raise SafetyError('duplicate_seed_product_requires_review')
            if rows:
                pid = rows[0][0]
            else:
                cur.execute("INSERT INTO products(name,price,stock,delivery,active) VALUES(?,1.0,0,'code_stock',1)", (name,))
                pid = cur.lastrowid
                conn.commit()
            cur.execute('SELECT price,active,delivery,fulfillment_type,fulfillment_config FROM products WHERE id=?', (pid,))
            price, active, mode, fulfillment, raw_config = cur.fetchone()
            if price != 1 or active != 1 or mode != 'code_stock' or fulfillment not in (None, 'code_stock') or raw_config not in (None, '', '{}'):
                raise SafetyError('seed_product_modified_requires_review')
            public = rdb.ensure_product_public_id(pid)
            cur.execute('SELECT COUNT(*) FROM product_stock WHERE product_id=?', (pid,))
            existing = cur.fetchone()[0]
            if existing > count:
                raise SafetyError('unexpected_seed_stock_count')
            while existing < count:
                end = min(count, existing + 500)
                for i in range(existing, end):
                    encrypted = encrypt_text(f'SYNTHETIC-NOT-VALID:{a.namespace}:{kind}:{i}', force=True)
                    if not encrypted.startswith('enc:'):
                        raise SafetyError('seed_delivery_encryption_unavailable')
                    cur.execute('INSERT INTO product_stock(product_id,delivery,sold) VALUES(?,?,0)', (pid, encrypted))
                conn.commit()
                existing = end
            return pid, public
        pid, public = product('history', a.stock)
        perf_pid, perf_public = product('performance', 5000)
        limited_pid, limited_public = product('limited', a.limited_stock)
        accounts = []
        # Extra dedicated load account; all accounts get representative history.
        # No Telegram links. TEST-only seed keys have their secrets discarded.
        for i in range(a.resellers + 1):
            name = stem + 'reseller:' + str(i)
            cur.execute('SELECT id,public_id,status FROM resellers WHERE name=?', (name,))
            rows = cur.fetchall()
            if len(rows) > 1:
                raise SafetyError('duplicate_seed_reseller_requires_review')
            if rows:
                rid, pub, status = rows[0]
                if status != 'active':
                    raise SafetyError('existing_seed_reseller_not_active')
            else:
                record = rdb.create_reseller(name)
                rid, pub = record['id'], record['public_id']
                rdb.set_reseller_status(rid, 'active')
            for allowed in (pid, perf_pid, limited_pid):
                rdb.set_product_access(rid, allowed, True)
                if not rdb.get_reseller_product_mapping(rid, allowed):
                    rdb.import_reseller_product(rid, allowed)
            # Generous but finite TEST-only funds posted through immutable ledger.
            credit = (plan['orders_per_reseller_max'] + 10000) * 100
            outcome, _ = rdb.post_wallet_transaction(rid, 'topup_credit', credit,
                f'loadseed.{a.namespace}.{i}', 'Synthetic staging seed')
            if outcome not in ('posted', 'duplicate'):
                raise SafetyError('seed_ledger_credit_rejected')
            key_db_id, key_public = 0, ''
            # Validation rechecks a REAL active TEST key for seed history.
            # Discard secrets immediately, never save them in reports.
            # The operator issues a separate load key in WebAdmin.
            cur.execute("SELECT id,key_id FROM reseller_api_keys WHERE reseller_id=? AND environment='test' AND status='active' AND enabled=1 ORDER BY id LIMIT 1", (rid,))
            key = cur.fetchone()
            if not key:
                key_public, secret = rdb.create_api_key(rid, 'test')
                del secret
                cur.execute('SELECT id,key_id FROM reseller_api_keys WHERE key_id=?', (key_public,))
                key = cur.fetchone()
            key_db_id, key_public = key
            accounts.append((rid, pub, key_db_id, key_public))
        for n in range(a.orders):
            rid, pub, key_db_id, key_public = accounts[n % len(accounts)]
            client = f'seed.{a.namespace}.{n}'
            if find_client_order(rid, client):
                continue
            context = ApiRequestContext(f'seed-{a.namespace}-{n}', 'test', rid, pub, key_db_id, key_public)
            validated = validate_order_request(context,
                {'product_id': public, 'quantity': 1, 'client_order_id': client}, 'test')
            pricing = calculate_order_price(validated)
            stock = reserve_and_validate_stock(validated, pricing)
            try:
                create_reseller_order(validated, pricing, stock, context)
            except BaseException:
                stock.release_stock()
                raise
            if (n + 1) % 1000 == 0:
                print(f'Seed progress: {n + 1}/{a.orders} orders')
        cur.execute('SELECT COUNT(*) FROM orders WHERE product_id=? AND source=?', (pid, 'reseller_api'))
        actual_orders = cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM product_stock WHERE product_id=?', (pid,))
        actual_stock = cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=1 AND order_id IS NULL', (pid,))
        orphan_claims = cur.fetchone()[0]
        if actual_orders != a.orders or actual_stock != a.stock or orphan_claims:
            raise SafetyError('seed_counts_or_stock_attribution_failed')
        from reseller_api.services.reconciliation import reconcile_reseller
        discrepancies = sum(len(reconcile_reseller(rid)) for rid, *_ in accounts)
        report = {'status': 'SEEDED' if not discrepancies else 'FAILED_RECONCILIATION',
                  'namespace': a.namespace, 'plan': plan, 'actual_orders': actual_orders,
                  'actual_stock': actual_stock, 'actual_resellers': len(accounts),
                  'orphan_claims': orphan_claims, 'reconciliation_discrepancies': discrepancies,
                  'database_fingerprint': database_fingerprint(cfg),
                  'load_reseller_public_id': accounts[-1][1],
                  'products': {'history': public, 'performance': perf_public, 'limited': limited_public},
                  'limited_stock': a.limited_stock,
                  'note': 'Create TEST key and adjust ONLY this account limits in WebAdmin.'}
        write_report(a.report, report)
        if discrepancies:
            return 1
        print('SEEDED: report written; no credentials printed or saved in report.')
if __name__ == '__main__':
    sys.exit(safe_main(main))
