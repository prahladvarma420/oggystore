"""Phase 9 Prompt 7 — Retry, backoff & dead-letter tests.

Test matrix
-----------
 1.  timeout              → retryable, outbox stays pending
 2.  500                  → retryable, outbox stays pending
 3.  503                  → retryable
 4.  429                  → retryable
 5.  400                  → terminal, outbox=failed immediately
 6.  401                  → terminal
 7.  404                  → terminal
 8.  retry count          → attempts counter increments correctly
 9.  backoff              → next_attempt_at increases with each attempt
10.  maximum attempts     → after MAX_ATTEMPTS, outbox=failed (dead-letter)
11.  dead letter          → status='failed', failure_reason persisted
12.  manual retry         → reset_for_manual_retry flips failed→pending, attempts=0
13.  disabled webhook     → no HTTP call, outbox=delivered
14.  deleted webhook      → no HTTP call, terminal, outbox=failed
15.  worker restart       → cleared claim allows re-delivery
16.  duplicate retry      → second claim_worker_job returns False; one delivery only

Invariants verified
-------------------
- no infinite loops  (MAX_ATTEMPTS enforced)
- no lost event      (pending/failed; never silently dropped)
- no duplicate business event (reset_for_manual_retry reuses existing row)
- no secret leakage  (captured log output checked for secret value)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
import types
import unittest
from io import StringIO
from unittest.mock import MagicMock, patch

BASE = '/data/oggystore-phase8'
if BASE not in sys.path:
    sys.path.insert(0, BASE)

# ---- DB singleton ----
_CONN = None
def _gc():   return _CONN
def _gcur(): return _CONN.cursor()

# ---- Stubs (must be before any project import) ----
db_mod = types.ModuleType('database.db')
db_mod.get_conn   = _gc
db_mod.get_cursor = _gcur
sys.modules.setdefault('database', types.ModuleType('database'))
sys.modules['database.db'] = db_mod

class _Atomic:
    def __init__(self, c): self._c = c
    def __enter__(self): return self
    def __exit__(self, et, ev, tb):
        if et: self._c.rollback()
        else:  self._c.commit()

pg_mod = types.ModuleType('database.pgcompat')
pg_mod.atomic = lambda c: _Atomic(c)
pg_mod.is_unique_violation = lambda e: False
sys.modules['database.pgcompat'] = pg_mod

tz_mod = types.ModuleType('utils.timezone')
tz_mod.now_text = lambda: '2026-01-01 00:00:00'
sys.modules.setdefault('utils', types.ModuleType('utils'))
sys.modules['utils.timezone'] = tz_mod

cr_mod = types.ModuleType('utils.crypto')
cr_mod.encrypt_text = lambda v: f'ENC:{v}'
cr_mod.decrypt_text = lambda v: v[4:] if str(v).startswith('ENC:') else v
sys.modules['utils.crypto'] = cr_mod

_CLAIMS: dict = {}
def _claim(job_key, worker_id, ttl_seconds=120):
    existing = _CLAIMS.get(job_key)
    if existing is None or existing == worker_id:
        _CLAIMS[job_key] = worker_id
        return True
    return False

ra_pkg = sys.modules.setdefault('reseller_api', types.ModuleType('reseller_api'))
ra_pkg.__path__ = [BASE + '/reseller_api']
ra_svc = sys.modules.setdefault('reseller_api.services', types.ModuleType('reseller_api.services'))
ra_svc.__path__ = [BASE + '/reseller_api/services']
idm_mod = types.ModuleType('reseller_api.services.order_idempotency')
idm_mod.claim_worker_job = _claim
idm_mod.WORKER_JOB_TTL_SECONDS = 300
sys.modules['reseller_api.services.order_idempotency'] = idm_mod

wk_pkg = types.ModuleType('reseller_api.workers')
wk_pkg.__path__ = [BASE + '/reseller_api/workers']
sys.modules['reseller_api.workers'] = wk_pkg

# ---- Import modules under test ----
import importlib
wd = importlib.import_module('reseller_api.services.webhook_delivery')
ww = importlib.import_module('reseller_api.workers.webhook_worker')

from reseller_api.services.webhook_delivery import ensure_delivery_log_table
from reseller_api.workers.webhook_worker import (
    _process_event, _schedule_retry, _mark_delivered, _mark_failed_terminal,
    _backoff_delay, _retry_at, is_retryable, reset_for_manual_retry,
    run_once, MAX_ATTEMPTS, BACKOFF_BASE_SECONDS, BACKOFF_CAP_SECONDS,
    RETRYABLE_HTTP_STATUSES, TERMINAL_HTTP_STATUSES,
)

# ---- Schema ----
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
        CREATE UNIQUE INDEX ux_ob
            ON webhook_outbox(reseller_id, event_type, aggregate_id);
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
                   aggregate_id='ORD-00000001', attempts=0):
    conn.execute(
        'INSERT INTO webhook_outbox(reseller_id,event_type,aggregate_id,payload_json,attempts) '
        'VALUES(?,?,?,?,?)',
        (reseller_id, event_type, aggregate_id,
         json.dumps({'event_type': event_type}), attempts),
    )
    conn.commit()
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]

def _insert_webhook(conn, reseller_id=1, url='https://h.example.com/',
                    secret='sec', enabled=1, events=None):
    ev = json.dumps(events) if events is not None else None
    conn.execute(
        'INSERT INTO reseller_webhooks(reseller_id,url,secret_encrypted,enabled,events) '
        'VALUES(?,?,?,?,?)',
        (reseller_id, url, f'ENC:{secret}', enabled, ev),
    )
    conn.commit()
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]

def _row(conn, oid):
    r = conn.execute(
        'SELECT id,reseller_id,event_type,aggregate_id,payload_json,attempts '
        'FROM webhook_outbox WHERE id=?', (oid,)
    ).fetchone()
    return dict(zip(['id','reseller_id','event_type','aggregate_id',
                     'payload_json','attempts'], r)) if r else None

def _status(conn, oid):
    r = conn.execute('SELECT status FROM webhook_outbox WHERE id=?', (oid,)).fetchone()
    return r[0] if r else None

def _attempts(conn, oid):
    r = conn.execute('SELECT attempts FROM webhook_outbox WHERE id=?', (oid,)).fetchone()
    return r[0] if r else None

def _next_at(conn, oid):
    r = conn.execute('SELECT next_attempt_at FROM webhook_outbox WHERE id=?', (oid,)).fetchone()
    return r[0] if r else None

def _failure_reason(conn, oid):
    try:
        r = conn.execute('SELECT failure_reason FROM webhook_outbox WHERE id=?', (oid,)).fetchone()
        return r[0] if r else None
    except Exception:
        return None

def _fake(code):
    r = MagicMock(); r.status_code = code
    r.raw = MagicMock(); r.raw.read = MagicMock(return_value=b'')
    return r


class RetryBackoffTests(unittest.TestCase):

    def setUp(self):
        global _CONN, _CLAIMS
        _CONN = _make_db()
        _CLAIMS.clear()
        ensure_delivery_log_table()
        for mod in (wd, ww):
            mod.get_conn   = _gc
            mod.get_cursor = _gcur
            mod.now_text   = lambda: '2026-01-01 00:00:00'
        ww.claim_worker_job = _claim
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

    # ================================================================ helpers
    def _run(self, oid, code=None, exc=None):
        """Run _process_event for oid with mocked HTTP."""
        row = _row(_CONN, oid)
        with patch('requests.post',
                   side_effect=exc if exc else None,
                   return_value=_fake(code) if code else None) as mp:
            _process_event(row)
            return mp

    # ================================================================ 1-4: retryable
    def test_timeout_retryable(self):
        from requests.exceptions import Timeout
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T001')
        _insert_webhook(_CONN)
        self._run(oid, exc=Timeout('to'))
        self.assertEqual(_status(_CONN, oid), 'pending')
        self.assertGreater(_attempts(_CONN, oid), 0)

    def test_500_retryable(self):
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T002')
        _insert_webhook(_CONN, url='https://e500.example.com/')
        self._run(oid, code=500)
        self.assertEqual(_status(_CONN, oid), 'pending')

    def test_503_retryable(self):
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T003')
        _insert_webhook(_CONN, url='https://e503.example.com/')
        self._run(oid, code=503)
        self.assertEqual(_status(_CONN, oid), 'pending')

    def test_429_retryable(self):
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T004')
        _insert_webhook(_CONN, url='https://rate.example.com/')
        self._run(oid, code=429)
        self.assertEqual(_status(_CONN, oid), 'pending')

    # ================================================================ 5-7: terminal
    def test_400_terminal(self):
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T005')
        _insert_webhook(_CONN, url='https://e400.example.com/')
        self._run(oid, code=400)
        self.assertEqual(_status(_CONN, oid), 'failed')

    def test_401_terminal(self):
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T006')
        _insert_webhook(_CONN, url='https://e401.example.com/')
        self._run(oid, code=401)
        self.assertEqual(_status(_CONN, oid), 'failed')

    def test_404_terminal(self):
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T007')
        _insert_webhook(_CONN, url='https://e404.example.com/')
        self._run(oid, code=404)
        self.assertEqual(_status(_CONN, oid), 'failed')

    # ================================================================ 8: retry count
    def test_retry_count_increments(self):
        """Each retryable failure increments attempts by 1."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T010')
        _insert_webhook(_CONN, url='https://cnt.example.com/')
        for expected_attempts in range(1, 4):
            row = _row(_CONN, oid)
            with patch('requests.post', return_value=_fake(500)):
                _process_event(row)
            _CLAIMS.clear()  # allow re-claiming for next attempt
            self.assertEqual(_attempts(_CONN, oid), expected_attempts,
                             f'Expected {expected_attempts} attempts after iteration')

    # ================================================================ 9: backoff
    def test_backoff_increases_with_attempt(self):
        """next_attempt_at grows as attempts increase (statistically)."""
        # Sample multiple delays; higher attempt should produce larger mean delay
        delays_0 = [_backoff_delay(0) for _ in range(50)]
        delays_5 = [_backoff_delay(5) for _ in range(50)]
        mean_0 = sum(delays_0) / len(delays_0)
        mean_5 = sum(delays_5) / len(delays_5)
        self.assertGreater(mean_5, mean_0,
            f'Higher attempt should have larger mean delay: {mean_0:.1f} vs {mean_5:.1f}')

    def test_backoff_capped(self):
        """Delay never exceeds BACKOFF_CAP_SECONDS."""
        for attempt in (0, 5, 10, 20, 100):
            for _ in range(10):
                d = _backoff_delay(attempt)
                self.assertLessEqual(d, BACKOFF_CAP_SECONDS,
                    f'Delay {d} exceeds cap {BACKOFF_CAP_SECONDS} at attempt {attempt}')

    def test_backoff_non_negative(self):
        """Delay is never negative."""
        for attempt in range(12):
            for _ in range(5):
                self.assertGreaterEqual(_backoff_delay(attempt), 0)

    def test_retry_at_is_string(self):
        ts = _retry_at(3)
        self.assertIsInstance(ts, str)
        self.assertGreater(len(ts), 10)

    # ================================================================ 10+11: max attempts / dead letter
    def test_max_attempts_dead_letters(self):
        """After MAX_ATTEMPTS failures, outbox is dead-lettered (status='failed')."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T020',
                             attempts=MAX_ATTEMPTS - 1)
        _insert_webhook(_CONN, url='https://max.example.com/')
        with patch('requests.post', return_value=_fake(500)):
            _process_event(_row(_CONN, oid))
        self.assertEqual(_status(_CONN, oid), 'failed',
            'Event must be dead-lettered after max attempts')

    def test_dead_letter_failure_reason_stored(self):
        """Dead-lettered rows must record a failure_reason (not secret data)."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T021',
                             attempts=MAX_ATTEMPTS - 1)
        _insert_webhook(_CONN, url='https://dl.example.com/')
        with patch('requests.post', return_value=_fake(500)):
            _process_event(_row(_CONN, oid))
        reason = _failure_reason(_CONN, oid)
        self.assertIsNotNone(reason, 'failure_reason should be stored')
        self.assertNotIn('secret', str(reason).lower(),
            'failure_reason must not contain secret data')

    def test_dead_letter_status_is_failed(self):
        """_mark_failed_terminal sets status='failed'."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T022')
        _mark_failed_terminal(oid, reason='test_reason')
        self.assertEqual(_status(_CONN, oid), 'failed')

    def test_no_infinite_loop(self):
        """A retryable failure can never loop more than MAX_ATTEMPTS times."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T023')
        _insert_webhook(_CONN, url='https://loop.example.com/')
        loop_count = 0
        while _status(_CONN, oid) == 'pending' and loop_count < MAX_ATTEMPTS + 5:
            _CLAIMS.clear()
            with patch('requests.post', return_value=_fake(500)):
                _process_event(_row(_CONN, oid))
            loop_count += 1
        final = _status(_CONN, oid)
        self.assertEqual(final, 'failed',
            f'Event should be dead-lettered, not still pending after {loop_count} loops')
        self.assertLessEqual(loop_count, MAX_ATTEMPTS + 1,
            'Should not take more than MAX_ATTEMPTS+1 iterations to dead-letter')

    # ================================================================ 12: manual retry
    def test_manual_retry_resets_failed_to_pending(self):
        """reset_for_manual_retry flips status=failed back to pending."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T030',
                             attempts=MAX_ATTEMPTS)
        _mark_failed_terminal(oid, reason='max_attempts')
        self.assertEqual(_status(_CONN, oid), 'failed')

        ok = reset_for_manual_retry(oid)
        self.assertTrue(ok, 'reset_for_manual_retry should succeed')
        self.assertEqual(_status(_CONN, oid), 'pending')
        self.assertEqual(_attempts(_CONN, oid), 0, 'attempts must be reset to 0')

    def test_manual_retry_does_not_create_new_row(self):
        """Manual retry must reuse the existing outbox row (no new business event)."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T031',
                             attempts=MAX_ATTEMPTS)
        _mark_failed_terminal(oid, reason='max_attempts')
        count_before = _CONN.execute(
            'SELECT COUNT(*) FROM webhook_outbox'
        ).fetchone()[0]

        reset_for_manual_retry(oid)

        count_after = _CONN.execute(
            'SELECT COUNT(*) FROM webhook_outbox'
        ).fetchone()[0]
        self.assertEqual(count_before, count_after,
            'No new outbox row must be created during manual retry')
        self.assertEqual(
            _CONN.execute(
                'SELECT aggregate_id FROM webhook_outbox WHERE id=?', (oid,)
            ).fetchone()[0],
            'ORD-T031',
            'Original aggregate_id must be preserved',
        )

    def test_manual_retry_on_pending_returns_false(self):
        """reset_for_manual_retry on a pending row must return False (safety guard)."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T032')
        ok = reset_for_manual_retry(oid)
        self.assertFalse(ok, 'Must not reset a row that is not in failed state')
        self.assertEqual(_status(_CONN, oid), 'pending')

    def test_manual_retry_on_delivered_returns_false(self):
        """reset_for_manual_retry on a delivered row must return False."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T033')
        _mark_delivered(oid)
        ok = reset_for_manual_retry(oid)
        self.assertFalse(ok)
        self.assertEqual(_status(_CONN, oid), 'delivered')

    def test_manual_retry_then_delivers_on_success(self):
        """After reset, a successful delivery marks the event delivered."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T034',
                             attempts=MAX_ATTEMPTS)
        _insert_webhook(_CONN, url='https://retry.example.com/')
        _mark_failed_terminal(oid, reason='max_attempts')
        reset_for_manual_retry(oid)
        self.assertEqual(_status(_CONN, oid), 'pending')
        with patch('requests.post', return_value=_fake(200)):
            run_once()
        self.assertEqual(_status(_CONN, oid), 'delivered')

    # ================================================================ 13: disabled webhook
    def test_disabled_webhook_marks_delivered_no_http(self):
        """Disabled webhook → no HTTP call; event marked delivered immediately."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T040')
        _insert_webhook(_CONN, enabled=0, url='https://off.example.com/')
        with patch('requests.post') as mp:
            _process_event(_row(_CONN, oid))
            mp.assert_not_called()
        self.assertEqual(_status(_CONN, oid), 'delivered')

    # ================================================================ 14: deleted webhook
    def test_deleted_webhook_terminal_no_retry(self):
        """No webhook rows at all → no HTTP; event marked delivered (no subs)."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T041')
        with patch('requests.post') as mp:
            _process_event(_row(_CONN, oid))
            mp.assert_not_called()
        # No active subs → delivered immediately (not failed)
        self.assertEqual(_status(_CONN, oid), 'delivered')

    def test_deleted_webhook_mid_flight_terminal(self):
        """If secret is None (webhook deleted between poll and delivery), terminal."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T042')
        _insert_webhook(_CONN, url='https://gone.example.com/')
        # Override secret fetcher to simulate deletion mid-flight
        wd.get_webhook_secret_by_id = lambda wid, rid: None
        try:
            with patch('requests.post') as mp:
                _process_event(_row(_CONN, oid))
                mp.assert_not_called()
            # All subs terminal (deleted) → dead-lettered
            self.assertEqual(_status(_CONN, oid), 'failed')
        finally:
            wd.get_webhook_secret_by_id = self._secret

    # ================================================================ 15: worker restart
    def test_worker_restart_can_redeliver(self):
        """Clearing claims simulates TTL expiry; a restarted worker delivers."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T050')
        _insert_webhook(_CONN, url='https://restart.example.com/')
        _CLAIMS[f'whk_deliver_{oid}'] = 'dead-worker'
        _CLAIMS.clear()  # TTL expired
        with patch('requests.post', return_value=_fake(200)):
            run_once()
        self.assertEqual(_status(_CONN, oid), 'delivered')

    # ================================================================ 16: duplicate retry
    def test_duplicate_worker_does_not_double_deliver(self):
        """Worker B cannot claim a job already held by Worker A."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T051')
        _insert_webhook(_CONN, url='https://dup.example.com/')
        orig = ww._WORKER_ID
        ww._WORKER_ID = 'worker-B'
        # Worker A already holds the claim
        _CLAIMS[f'whk_deliver_{oid}'] = 'worker-A'
        try:
            with patch('requests.post') as mp:
                _process_event(_row(_CONN, oid))
                mp.assert_not_called()
        finally:
            ww._WORKER_ID = orig
        self.assertEqual(_status(_CONN, oid), 'pending',
            'Outbox must remain pending when Worker B is blocked')

    # ================================================================ is_retryable unit tests
    def test_is_retryable_retryable_codes(self):
        for code in (408, 429, 500, 502, 503, 504):
            r = {'success': False, 'error_category': '5xx' if code >= 500 else '4xx',
                 'http_status': code}
            self.assertTrue(is_retryable(r), f'{code} should be retryable')

    def test_is_retryable_terminal_codes(self):
        for code in (400, 401, 403, 404, 410):
            r = {'success': False, 'error_category': '4xx', 'http_status': code}
            self.assertFalse(is_retryable(r), f'{code} should be terminal')

    def test_is_retryable_success(self):
        self.assertFalse(is_retryable({'success': True}))

    def test_is_retryable_deleted(self):
        self.assertFalse(is_retryable({'success': False, 'error_category': 'deleted'}))

    def test_is_retryable_3xx(self):
        self.assertFalse(is_retryable({'success': False, 'error_category': '3xx',
                                       'http_status': 301}))

    def test_is_retryable_timeout(self):
        self.assertTrue(is_retryable({'success': False, 'error_category': 'timeout'}))

    def test_is_retryable_connection(self):
        self.assertTrue(is_retryable({'success': False, 'error_category': 'connection'}))

    # ================================================================ no secret leakage
    def test_no_secret_in_worker_logs(self):
        """Webhook signing secret must never appear in worker log output."""
        secret = 'MY_VERY_SECRET_KEY_12345_DO_NOT_LOG'
        oid = _insert_outbox(_CONN, aggregate_id='ORD-T060')
        _insert_webhook(_CONN, url='https://sec.example.com/', secret=secret)

        log_stream = StringIO()
        handler = logging.StreamHandler(log_stream)
        handler.setLevel(logging.DEBUG)
        loggers = [
            logging.getLogger('reseller_api.workers.webhook_worker'),
            logging.getLogger('reseller_api.services.webhook_delivery'),
        ]
        for lg in loggers:
            lg.addHandler(handler)
            lg.setLevel(logging.DEBUG)
        try:
            with patch('requests.post', return_value=_fake(200)):
                run_once()
        finally:
            for lg in loggers:
                lg.removeHandler(handler)

        out = log_stream.getvalue()
        self.assertNotIn(secret, out,
            f'Secret must not appear in logs.\nCaptured:\n{out[:500]}')

    # ================================================================ retryable set sanity
    def test_retryable_terminal_sets_disjoint(self):
        """No status code can be both retryable and terminal."""
        overlap = RETRYABLE_HTTP_STATUSES & TERMINAL_HTTP_STATUSES
        self.assertEqual(overlap, frozenset(),
            f'Codes appear in both retryable and terminal sets: {overlap}')

    # ================================================================ no event duplication
    def test_unique_index_prevents_duplicate_business_event(self):
        """The UNIQUE INDEX on webhook_outbox prevents duplicate event rows."""
        oid1 = _insert_outbox(_CONN, aggregate_id='ORD-T070', event_type='order.created')
        # Attempt to insert the same logical event again
        try:
            _CONN.execute(
                'INSERT INTO webhook_outbox(reseller_id,event_type,aggregate_id,payload_json) '
                'VALUES(?,?,?,?)',
                (1, 'order.created', 'ORD-T070', '{}')
            )
            _CONN.commit()
            duplicate_inserted = True
        except Exception:
            duplicate_inserted = False
        self.assertFalse(duplicate_inserted,
            'Unique index must prevent duplicate business event rows')
        count = _CONN.execute('SELECT COUNT(*) FROM webhook_outbox').fetchone()[0]
        self.assertEqual(count, 1, 'Only one row must exist for this event')


if __name__ == '__main__':
    unittest.main()
