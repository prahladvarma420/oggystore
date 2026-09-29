"""Phase 9 Prompt 5 — Webhook delivery worker tests.

All HTTP calls are intercepted; no real network traffic.
All DB I/O uses in-memory SQLite.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

BASE = '/data/oggystore-phase8'
if BASE not in sys.path:
    sys.path.insert(0, BASE)

# ---------------------------------------------------------------------------
# In-memory DB singleton
# ---------------------------------------------------------------------------
_CONN = None
def _gc(): return _CONN
def _gcur(): return _CONN.cursor()

# ---------------------------------------------------------------------------
# Stubs installed BEFORE any project imports
# ---------------------------------------------------------------------------

# database.db
db_mod = types.ModuleType('database.db')
db_mod.get_conn = _gc
db_mod.get_cursor = _gcur
sys.modules.setdefault('database', types.ModuleType('database'))
sys.modules['database.db'] = db_mod

# database.pgcompat
pg_mod = types.ModuleType('database.pgcompat')
class _Atomic:
    def __init__(self, c): self._c = c
    def __enter__(self): return self
    def __exit__(self, et, ev, tb):
        if et: self._c.rollback()
        else:  self._c.commit()
pg_mod.atomic = lambda c: _Atomic(c)
pg_mod.is_unique_violation = lambda e: False
sys.modules['database.pgcompat'] = pg_mod

# utils.timezone
tz_mod = types.ModuleType('utils.timezone')
tz_mod.now_text = lambda: '2026-01-01 00:00:00'
sys.modules.setdefault('utils', types.ModuleType('utils'))
sys.modules['utils.timezone'] = tz_mod

# utils.crypto
cr_mod = types.ModuleType('utils.crypto')
cr_mod.encrypt_text = lambda v: f'ENC:{v}'
cr_mod.decrypt_text = lambda v: v[4:] if str(v).startswith('ENC:') else v
sys.modules['utils.crypto'] = cr_mod

# reseller_api.services.order_idempotency  (claim_worker_job stub)
_CLAIMS: dict = {}
def _claim(job_key, worker_id, ttl_seconds=120):
    existing = _CLAIMS.get(job_key)
    if existing is None or existing == worker_id:
        _CLAIMS[job_key] = worker_id
        return True
    return False

idm_pkg = sys.modules.setdefault('reseller_api', types.ModuleType('reseller_api'))
idm_pkg.__path__ = [BASE + '/reseller_api']
idm_svc = sys.modules.setdefault('reseller_api.services', types.ModuleType('reseller_api.services'))
idm_svc.__path__ = [BASE + '/reseller_api/services']
idm_mod = types.ModuleType('reseller_api.services.order_idempotency')
idm_mod.claim_worker_job = _claim
idm_mod.WORKER_JOB_TTL_SECONDS = 300
sys.modules['reseller_api.services.order_idempotency'] = idm_mod

# reseller_api.workers package stub
wk_pkg = types.ModuleType('reseller_api.workers')
wk_pkg.__path__ = [BASE + '/reseller_api/workers']
sys.modules['reseller_api.workers'] = wk_pkg

# ---------------------------------------------------------------------------
# Import real modules
# ---------------------------------------------------------------------------
import importlib
wd = importlib.import_module('reseller_api.services.webhook_delivery')
ww = importlib.import_module('reseller_api.workers.webhook_worker')

from reseller_api.services.webhook_delivery import (
    deliver_to_webhook, get_active_subscriptions, get_attempt_count,
    record_delivery_attempt, get_delivery_log_for_event,
    ensure_delivery_log_table, _classify, _sign_payload,
    ERR_TIMEOUT, ERR_CONNECTION, ERR_TLS, ERR_4XX, ERR_5XX, ERR_3XX,
)
from reseller_api.workers.webhook_worker import (
    run_once, _poll_pending_events, _mark_delivered,
    _mark_failed_terminal, _schedule_retry, _process_event,
)

# ---------------------------------------------------------------------------
# DB schema helpers
# ---------------------------------------------------------------------------

def _make_db():
    conn = sqlite3.connect(':memory:', check_same_thread=False)
    conn.executescript("""
        CREATE TABLE reseller_webhooks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reseller_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            secret_encrypted TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            events TEXT
        );
        CREATE TABLE webhook_outbox (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            reseller_id     INTEGER NOT NULL,
            event_type      TEXT    NOT NULL,
            aggregate_id    TEXT    NOT NULL,
            payload_json    TEXT    NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'pending',
            attempts        INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT    NOT NULL DEFAULT '2026-01-01 00:00:00',
            created_at      TEXT    NOT NULL DEFAULT (CURRENT_TIMESTAMP),
            processed_at    TEXT
        );
        CREATE UNIQUE INDEX ux_ob ON webhook_outbox(reseller_id,event_type,aggregate_id);
        CREATE TABLE worker_jobs (
            job_key TEXT PRIMARY KEY,
            worker_id TEXT NOT NULL,
            claimed_at INTEGER NOT NULL,
            heartbeat_at INTEGER NOT NULL
        );
    """)
    conn.commit()
    return conn


def _insert_outbox(conn, reseller_id=1, event_type='order.created',
                   aggregate_id='ORD-00000001', payload=None, attempts=0):
    p = payload or {'event_type': event_type, 'order_id': aggregate_id}
    conn.execute(
        'INSERT INTO webhook_outbox(reseller_id,event_type,aggregate_id,payload_json,attempts) '
        'VALUES(?,?,?,?,?)',
        (reseller_id, event_type, aggregate_id, json.dumps(p), attempts),
    )
    conn.commit()
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]


def _insert_webhook(conn, reseller_id=1, url='https://example.com/hook',
                    secret='testsecret', enabled=1, events=None):
    ev = json.dumps(events) if events is not None else None
    conn.execute(
        'INSERT INTO reseller_webhooks(reseller_id,url,secret_encrypted,enabled,events) '
        'VALUES(?,?,?,?,?)',
        (reseller_id, url, f'ENC:{secret}', enabled, ev),
    )
    conn.commit()
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]


def _status(conn, oid):
    r = conn.execute('SELECT status FROM webhook_outbox WHERE id=?', (oid,)).fetchone()
    return r[0] if r else None


def _logs(conn, oid):
    return conn.execute(
        'SELECT * FROM webhook_delivery_log WHERE outbox_event_id=?', (oid,)
    ).fetchall()


def _fake_resp(code):
    r = MagicMock()
    r.status_code = code
    r.raw = MagicMock()
    r.raw.read = MagicMock(return_value=b'')
    return r


def _row(conn, oid):
    r = conn.execute(
        'SELECT id,reseller_id,event_type,aggregate_id,payload_json,attempts '
        'FROM webhook_outbox WHERE id=?', (oid,)
    ).fetchone()
    return dict(zip(['id','reseller_id','event_type','aggregate_id','payload_json','attempts'], r))


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class WebhookDeliveryWorkerTests(unittest.TestCase):

    def setUp(self):
        global _CONN, _CLAIMS
        _CONN = _make_db()
        _CLAIMS.clear()
        ensure_delivery_log_table()
        # Patch module-level references to use in-memory DB
        for mod in (wd, ww):
            mod.get_conn   = _gc
            mod.get_cursor = _gcur
            mod.now_text   = lambda: '2026-01-01 00:00:00'
        ww.claim_worker_job = _claim
        # Patch secret fetcher to use in-memory DB
        wd.get_webhook_secret_by_id = self._secret

    def _secret(self, webhook_id, reseller_id):
        r = _CONN.execute(
            'SELECT secret_encrypted FROM reseller_webhooks WHERE id=? AND reseller_id=?',
            (webhook_id, reseller_id)
        ).fetchone()
        if not r: return None
        v = r[0]; return v[4:] if str(v).startswith('ENC:') else v

    def tearDown(self):
        _CONN.close()

    # ------------------------------------------------------------------ 1+2
    def test_200_marks_delivered(self):
        """HTTP 200 -> outbox row status becomes 'delivered'."""
        oid = _insert_outbox(_CONN)
        _insert_webhook(_CONN)
        with patch('requests.post', return_value=_fake_resp(200)):
            run_once()
        self.assertEqual(_status(_CONN, oid), 'delivered')

    def test_201_marks_delivered(self):
        """HTTP 201 is also a success."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000002')
        _insert_webhook(_CONN)
        with patch('requests.post', return_value=_fake_resp(201)):
            run_once()
        self.assertEqual(_status(_CONN, oid), 'delivered')

    # ------------------------------------------------------------------ 3-5: terminal 4xx
    def _terminal_4xx(self, code, agg):
        oid = _insert_outbox(_CONN, aggregate_id=agg)
        _insert_webhook(_CONN, url=f'https://x.example.com/{code}')
        with patch('requests.post', return_value=_fake_resp(code)):
            run_once()
        return oid

    def test_400_terminal(self):
        self.assertEqual(_status(_CONN, self._terminal_4xx(400,'ORD-00000010')), 'failed')

    def test_401_terminal(self):
        self.assertEqual(_status(_CONN, self._terminal_4xx(401,'ORD-00000011')), 'failed')

    def test_404_terminal(self):
        self.assertEqual(_status(_CONN, self._terminal_4xx(404,'ORD-00000012')), 'failed')

    # ------------------------------------------------------------------ 6: 429 retryable
    def test_429_retryable(self):
        """HTTP 429 is NOT in the terminal-4xx list; should schedule retry."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000020')
        _insert_webhook(_CONN, url='https://rate.example.com/')
        with patch('requests.post', return_value=_fake_resp(429)):
            run_once()
        self.assertEqual(_status(_CONN, oid), 'pending')
        row = _CONN.execute('SELECT attempts FROM webhook_outbox WHERE id=?', (oid,)).fetchone()
        self.assertGreater(row[0], 0)

    # ------------------------------------------------------------------ 7: 500 retryable
    def test_500_retryable(self):
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000021')
        _insert_webhook(_CONN, url='https://err.example.com/')
        with patch('requests.post', return_value=_fake_resp(500)):
            run_once()
        self.assertEqual(_status(_CONN, oid), 'pending')

    # ------------------------------------------------------------------ 8: timeout
    def test_timeout_error_category(self):
        from requests.exceptions import Timeout
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000030')
        _insert_webhook(_CONN, url='https://slow.example.com/')
        with patch('requests.post', side_effect=Timeout('timed out')):
            run_once()
        lg = _logs(_CONN, oid)
        self.assertTrue(len(lg) > 0)
        cols = ['id','outbox_event_id','webhook_id','reseller_id','attempt',
                'request_id','requested_at','responded_at','http_status',
                'success','error_category','next_retry_at','created_at']
        d = dict(zip(cols, lg[0]))
        self.assertEqual(d['error_category'], ERR_TIMEOUT)
        self.assertEqual(d['success'], 0)

    # ------------------------------------------------------------------ 9: connection error
    def test_connection_error_category(self):
        from requests.exceptions import ConnectionError as CE
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000031')
        _insert_webhook(_CONN, url='https://gone.example.com/')
        with patch('requests.post', side_effect=CE('refused')):
            run_once()
        lg = _logs(_CONN, oid)
        self.assertTrue(len(lg) > 0)
        cols = ['id','outbox_event_id','webhook_id','reseller_id','attempt',
                'request_id','requested_at','responded_at','http_status',
                'success','error_category','next_retry_at','created_at']
        d = dict(zip(cols, lg[0]))
        self.assertEqual(d['error_category'], ERR_CONNECTION)

    # ------------------------------------------------------------------ 10: worker restart
    def test_worker_restart_reprocesses(self):
        """Claim expiry (simulated by clearing _CLAIMS) lets a new worker deliver."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000040')
        _insert_webhook(_CONN, url='https://restart.example.com/')
        # Simulate old worker held claim but crashed
        _CLAIMS[f'whk_deliver_{oid}'] = 'dead-worker'
        _CLAIMS.clear()   # TTL expired
        with patch('requests.post', return_value=_fake_resp(200)):
            run_once()
        self.assertEqual(_status(_CONN, oid), 'delivered')

    # ------------------------------------------------------------------ 11: duplicate worker
    def test_duplicate_worker_skipped(self):
        """Worker B must not send HTTP if Worker A holds the claim."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000041')
        _insert_webhook(_CONN, url='https://dup.example.com/')
        _CLAIMS[f'whk_deliver_{oid}'] = 'worker-A'
        orig = ww._WORKER_ID
        ww._WORKER_ID = 'worker-B'
        try:
            with patch('requests.post') as mp:
                _process_event(_row(_CONN, oid))
                mp.assert_not_called()
        finally:
            ww._WORKER_ID = orig
        self.assertEqual(_status(_CONN, oid), 'pending')

    # ------------------------------------------------------------------ 12: disabled webhook
    def test_disabled_webhook_no_http(self):
        """Disabled webhook -> no HTTP call; event marked delivered (no active subs)."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000050')
        _insert_webhook(_CONN, url='https://off.example.com/', enabled=0)
        with patch('requests.post') as mp:
            run_once()
            mp.assert_not_called()
        self.assertEqual(_status(_CONN, oid), 'delivered')

    # ------------------------------------------------------------------ 13: deleted webhook
    def test_deleted_webhook_graceful(self):
        """No webhook rows -> zero subscriptions -> mark delivered immediately."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000051')
        with patch('requests.post') as mp:
            run_once()
            mp.assert_not_called()
        self.assertEqual(_status(_CONN, oid), 'delivered')

    # ------------------------------------------------------------------ 14: multiple endpoints
    def test_multiple_endpoints_all_called(self):
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000060')
        _insert_webhook(_CONN, url='https://ep1.example.com/')
        _insert_webhook(_CONN, url='https://ep2.example.com/')
        with patch('requests.post', return_value=_fake_resp(200)) as mp:
            run_once()
        self.assertEqual(mp.call_count, 2)
        self.assertEqual(_status(_CONN, oid), 'delivered')

    # ------------------------------------------------------------------ 15: multiple events
    def test_multiple_events_independent(self):
        oid1 = _insert_outbox(_CONN, aggregate_id='ORD-00000070', event_type='order.created')
        oid2 = _insert_outbox(_CONN, aggregate_id='ORD-00000070', event_type='delivery.pending')
        _insert_webhook(_CONN)
        with patch('requests.post', return_value=_fake_resp(200)):
            run_once()
        self.assertEqual(_status(_CONN, oid1), 'delivered')
        self.assertEqual(_status(_CONN, oid2), 'delivered')

    # ------------------------------------------------------------------ 16: delivery log fields
    def test_delivery_log_all_fields(self):
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000080')
        _insert_webhook(_CONN)
        with patch('requests.post', return_value=_fake_resp(200)):
            run_once()
        lg = _logs(_CONN, oid)
        self.assertEqual(len(lg), 1)
        cols = ['id','outbox_event_id','webhook_id','reseller_id','attempt',
                'request_id','requested_at','responded_at','http_status',
                'success','error_category','next_retry_at','created_at']
        d = dict(zip(cols, lg[0]))
        self.assertEqual(d['outbox_event_id'], oid)
        self.assertEqual(d['http_status'], 200)
        self.assertEqual(d['success'], 1)
        self.assertIsNone(d['error_category'])
        self.assertIsNotNone(d['request_id'])
        self.assertIsNotNone(d['requested_at'])
        self.assertIsNotNone(d['responded_at'])
        self.assertGreaterEqual(d['attempt'], 1)

    # ------------------------------------------------------------------ HMAC signing
    def test_hmac_signature_stable(self):
        s1 = _sign_payload('sec', b'{"x":1}', '1700000000')
        s2 = _sign_payload('sec', b'{"x":1}', '1700000000')
        self.assertEqual(s1, s2)
        self.assertTrue(s1.startswith('sha256='))

    def test_hmac_different_secret(self):
        s1 = _sign_payload('A', b'body', '1000')
        s2 = _sign_payload('B', b'body', '1000')
        self.assertNotEqual(s1, s2)

    # ------------------------------------------------------------------ response classify
    def test_classify_2xx_success(self):
        for c in (200, 201, 204):
            ok, cat = _classify(c)
            self.assertTrue(ok, f'{c} should succeed')
            self.assertIsNone(cat)

    def test_classify_3xx_fail(self):
        ok, cat = _classify(301)
        self.assertFalse(ok); self.assertEqual(cat, ERR_3XX)

    def test_classify_4xx_fail(self):
        ok, cat = _classify(422)
        self.assertFalse(ok); self.assertEqual(cat, ERR_4XX)

    def test_classify_5xx_fail(self):
        ok, cat = _classify(503)
        self.assertFalse(ok); self.assertEqual(cat, ERR_5XX)

    def test_classify_none_fail(self):
        ok, cat = _classify(None)
        self.assertFalse(ok); self.assertIsNotNone(cat)

    # ------------------------------------------------------------------ event-type filter
    def test_event_type_filter(self):
        _insert_webhook(_CONN, url='https://f1.example.com/', events=['order.created'])
        self.assertEqual(len(get_active_subscriptions(1, 'order.created')), 1)
        self.assertEqual(len(get_active_subscriptions(1, 'delivery.pending')), 0)

    def test_null_events_matches_all(self):
        _insert_webhook(_CONN, url='https://f2.example.com/', events=None)
        for et in ('order.created', 'delivery.pending', 'refund.created'):
            self.assertEqual(len(get_active_subscriptions(1, et)), 1, et)

    # ------------------------------------------------------------------ cross-reseller
    def test_event_not_delivered_to_wrong_reseller(self):
        """Webhook for reseller 2 must not receive reseller 1's events."""
        oid = _insert_outbox(_CONN, reseller_id=1, aggregate_id='ORD-00000090')
        _insert_webhook(_CONN, reseller_id=2, url='https://r2.example.com/')
        with patch('requests.post') as mp:
            run_once()
            mp.assert_not_called()   # reseller 2's hook must not fire for reseller 1
        # No active subs for reseller 1 -> mark delivered
        self.assertEqual(_status(_CONN, oid), 'delivered')

if __name__ == '__main__':
    unittest.main()
