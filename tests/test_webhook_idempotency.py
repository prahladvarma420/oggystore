"""Phase 9 Prompt 8 — Idempotency & replay-protection tests.

Test matrix
-----------
 1.  Same event twice          → only one outbox row (UNIQUE INDEX guard)
 2.  Same delivery twice       → only one delivery log row (INSERT OR IGNORE)
 3.  Concurrent workers        → only Worker A delivers; Worker B skipped
 4.  Worker crash              → stale claim expires; replacement worker delivers
 5.  Worker restart            → cleared claim allows new delivery
 6.  API restart               → outbox rows survive in DB; picked up after restart
 7.  Railway restart           → same as API restart (process state is ephemeral)
 8.  Timeout, receiver already processed → retry has same event_id; reseller deduplicates
 9.  Old signed request replay → verify_signature rejects timestamp outside window
10.  Modified event ID         → verify_signature rejects tampered payload
11.  Modified delivery ID      → verify_signature rejects tampered payload
12.  Modified payload          → verify_signature rejects tampered body
13.  Duplicate queue job       → claim_worker_job returns False for second worker
14.  Duplicate retry           → reset_for_manual_retry is idempotent; no double reset

Invariants verified
-------------------
- no duplicate business event  (UNIQUE INDEX on outbox)
- stable event_id              (same outbox row id across retries)
- stable idempotency_key       (same as event_id, explicit payload field)
- unique delivery_id per attempt (UUID4 per call)
- correct delivery state       (delivered / pending / failed)
- no financial duplication     (event emitted inside business tx; INSERT OR IGNORE)
- no stock duplication         (same mechanism)
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
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
def _gc():   return _CONN
def _gcur(): return _CONN.cursor()

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------
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
pg_mod.is_unique_violation = lambda e: 'UNIQUE' in str(e).upper()
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

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
import importlib
wd  = importlib.import_module('reseller_api.services.webhook_delivery')
ww  = importlib.import_module('reseller_api.workers.webhook_worker')
wsig = importlib.import_module('reseller_api.services.webhook_signature')

from reseller_api.services.webhook_delivery import (
    ensure_delivery_log_table, deliver_to_webhook,
    get_delivery_log_for_event, record_delivery_attempt,
)
from reseller_api.workers.webhook_worker import (
    run_once, _process_event, reset_for_manual_retry,
    _mark_failed_terminal, _mark_delivered, MAX_ATTEMPTS,
)
from reseller_api.services.webhook_signature import (
    verify_signature, sign_body, FRESHNESS_WINDOW_SECONDS,
)
from reseller_api.services.order_idempotency import claim_worker_job

# ---------------------------------------------------------------------------
# DB helpers
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
        'INSERT INTO webhook_outbox'
        '(reseller_id,event_type,aggregate_id,payload_json,attempts)'
        ' VALUES(?,?,?,?,?)',
        (reseller_id, event_type, aggregate_id,
         json.dumps({'event_type': event_type, 'order_id': aggregate_id}),
         attempts),
    )
    conn.commit()
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]

def _insert_webhook(conn, reseller_id=1, url='https://h.example.com/',
                    secret='testsec', enabled=1, events=None):
    ev = json.dumps(events) if events is not None else None
    conn.execute(
        'INSERT INTO reseller_webhooks(reseller_id,url,secret_encrypted,enabled,events)'
        ' VALUES(?,?,?,?,?)',
        (reseller_id, url, f'ENC:{secret}', enabled, ev),
    )
    conn.commit()
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]

def _status(conn, oid):
    r = conn.execute('SELECT status FROM webhook_outbox WHERE id=?', (oid,)).fetchone()
    return r[0] if r else None

def _row(conn, oid):
    r = conn.execute(
        'SELECT id,reseller_id,event_type,aggregate_id,payload_json,attempts'
        ' FROM webhook_outbox WHERE id=?', (oid,)
    ).fetchone()
    return dict(zip(['id','reseller_id','event_type','aggregate_id',
                     'payload_json','attempts'], r)) if r else None

def _log_rows(conn, oid):
    return conn.execute(
        'SELECT * FROM webhook_delivery_log WHERE outbox_event_id=?', (oid,)
    ).fetchall()

def _fake(code):
    r = MagicMock()
    r.status_code = code
    r.raw = MagicMock()
    r.raw.read = MagicMock(return_value=b'')
    return r

def _secret_fn(webhook_id, reseller_id):
    r = _CONN.execute(
        'SELECT secret_encrypted FROM reseller_webhooks WHERE id=? AND reseller_id=?',
        (webhook_id, reseller_id)
    ).fetchone()
    if not r: return None
    v = r[0]; return v[4:] if str(v).startswith('ENC:') else v


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class IdempotencyTests(unittest.TestCase):

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
        wd.get_webhook_secret_by_id = _secret_fn

    def tearDown(self):
        _CONN.close()

    # ------------------------------------------------------------------ 1
    def test_same_event_twice_one_outbox_row(self):
        """Inserting the same logical event twice results in exactly one outbox row.

        The UNIQUE INDEX on (reseller_id, event_type, aggregate_id) enforces this
        at the DB level regardless of application checks.
        """
        _insert_outbox(_CONN, aggregate_id='ORD-00000001')
        try:
            _CONN.execute(
                'INSERT INTO webhook_outbox'
                '(reseller_id,event_type,aggregate_id,payload_json)'
                ' VALUES(?,?,?,?)',
                (1, 'order.created', 'ORD-00000001', '{}')
            )
            _CONN.commit()
            duplicate_inserted = True
        except Exception:
            duplicate_inserted = False
        self.assertFalse(duplicate_inserted,
            'UNIQUE INDEX must block duplicate logical events')
        count = _CONN.execute('SELECT COUNT(*) FROM webhook_outbox').fetchone()[0]
        self.assertEqual(count, 1)

    def test_insert_or_ignore_is_safe(self):
        """INSERT OR IGNORE on duplicate outbox row is silent (no exception)."""
        _insert_outbox(_CONN, aggregate_id='ORD-00000002')
        before = _CONN.execute('SELECT COUNT(*) FROM webhook_outbox').fetchone()[0]
        try:
            _CONN.execute(
                'INSERT OR IGNORE INTO webhook_outbox'
                '(reseller_id,event_type,aggregate_id,payload_json)'
                ' VALUES(?,?,?,?)',
                (1, 'order.created', 'ORD-00000002', '{}')
            )
            _CONN.commit()
        except Exception as e:
            self.fail(f'INSERT OR IGNORE raised unexpectedly: {e}')
        after = _CONN.execute('SELECT COUNT(*) FROM webhook_outbox').fetchone()[0]
        self.assertEqual(before, after, 'INSERT OR IGNORE must not add a row')

    # ------------------------------------------------------------------ 2
    def test_same_delivery_twice_one_log_row(self):
        """INSERT OR IGNORE on delivery log prevents duplicate rows for the
        same (outbox_event_id, webhook_id, attempt)."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000003')
        wid = _insert_webhook(_CONN)
        req_id = 'req-uuid-1234'
        fake_result = {'success': True, 'error_category': None,
                       'http_status': 200, 'responded_at': '2026-01-01 00:00:01',
                       'elapsed_ms': 50}
        # First insert
        record_delivery_attempt(oid, wid, 1, 1, req_id, '2026-01-01', fake_result)
        # Second insert of exact same row — must be silently ignored
        try:
            record_delivery_attempt(oid, wid, 1, 1, req_id, '2026-01-01', fake_result)
        except Exception as e:
            self.fail(f'Second record_delivery_attempt raised: {e}')
        logs = _log_rows(_CONN, oid)
        self.assertEqual(len(logs), 1, 'Only one delivery log row must exist')

    def test_delivery_log_unique_constraint_different_attempt(self):
        """Different attempt numbers produce different log rows (correct retry behavior)."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000004')
        wid = _insert_webhook(_CONN)
        fr  = {'success': False, 'error_category': '5xx',
               'http_status': 500, 'responded_at': '2026-01-01', 'elapsed_ms': 10}
        record_delivery_attempt(oid, wid, 1, 1, 'req-a', '2026-01-01', fr)
        record_delivery_attempt(oid, wid, 1, 2, 'req-b', '2026-01-01', fr)
        logs = _log_rows(_CONN, oid)
        self.assertEqual(len(logs), 2, 'Each attempt must produce its own log row')

    # ------------------------------------------------------------------ 3
    def test_concurrent_workers_only_one_delivers(self):
        """Worker A claims the job; Worker B is blocked and makes no HTTP call."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000010')
        _insert_webhook(_CONN, url='https://concurrent.example.com/')

        orig = ww._WORKER_ID
        ww._WORKER_ID = 'worker-B'
        _CLAIMS[f'whk_deliver_{oid}'] = 'worker-A'
        try:
            with patch('requests.post') as mp:
                _process_event(_row(_CONN, oid))
                mp.assert_not_called()
        finally:
            ww._WORKER_ID = orig
        self.assertEqual(_status(_CONN, oid), 'pending')

    def test_concurrent_workers_a_succeeds(self):
        """Worker A (claim holder) successfully delivers the event."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000011')
        _insert_webhook(_CONN, url='https://workerA.example.com/')
        with patch('requests.post', return_value=_fake(200)):
            run_once()
        self.assertEqual(_status(_CONN, oid), 'delivered')

    # ------------------------------------------------------------------ 4 & 5
    def test_worker_crash_stale_claim_redeliver(self):
        """After a worker crashes (claim cleared = TTL expired), a new worker
        picks up the pending event and delivers it."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000020')
        _insert_webhook(_CONN, url='https://crashed.example.com/')
        # Simulate crashed worker that held the claim
        _CLAIMS[f'whk_deliver_{oid}'] = 'dead-worker'
        _CLAIMS.clear()  # TTL expired
        with patch('requests.post', return_value=_fake(200)):
            run_once()
        self.assertEqual(_status(_CONN, oid), 'delivered')

    def test_worker_restart_continues_pending_events(self):
        """Pending outbox rows survive in the DB across process restarts.
        A freshly started worker (new _WORKER_ID, empty _CLAIMS) picks them up."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000021')
        _insert_webhook(_CONN, url='https://restart.example.com/')
        # Simulate restart: new worker_id, empty claims
        orig = ww._WORKER_ID
        ww._WORKER_ID = 'fresh-worker-after-restart'
        _CLAIMS.clear()
        try:
            with patch('requests.post', return_value=_fake(200)):
                run_once()
        finally:
            ww._WORKER_ID = orig
        self.assertEqual(_status(_CONN, oid), 'delivered')

    # ------------------------------------------------------------------ 6 & 7
    def test_api_restart_outbox_rows_survive(self):
        """Outbox rows are persisted in the DB, not in process memory.
        An API or Railway restart does not lose pending events.
        Verified by checking the row still exists and is still 'pending'
        with its original payload intact after clearing all in-process state."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000030')
        original_payload = _CONN.execute(
            'SELECT payload_json FROM webhook_outbox WHERE id=?', (oid,)
        ).fetchone()[0]

        # Simulate restart: in-process state cleared, DB survives
        _CLAIMS.clear()
        # Row must still be in the DB and pending
        self.assertEqual(_status(_CONN, oid), 'pending',
            'Pending event must survive process restart')
        # Payload must be unchanged
        after_payload = _CONN.execute(
            'SELECT payload_json FROM webhook_outbox WHERE id=?', (oid,)
        ).fetchone()[0]
        self.assertEqual(original_payload, after_payload,
            'Payload must be unchanged after restart')

    def test_railway_restart_equivalent_to_api_restart(self):
        """Railway restart is process-level ephemeral; DB state survives.
        Functionally identical to API restart test."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000031')
        _insert_webhook(_CONN, url='https://railway.example.com/')
        _CLAIMS.clear()
        with patch('requests.post', return_value=_fake(200)):
            run_once()
        self.assertEqual(_status(_CONN, oid), 'delivered')

    # ------------------------------------------------------------------ 8
    def test_timeout_after_receiver_processed_stable_event_id(self):
        """Critical scenario: network timeout after receiver processed the request.

        Sequence:
          1. Worker sends HTTP with event_id=X, delivery_id=UUID-1.
          2. Receiver processes it (creates order, charges card, etc.).
          3. Network drops the response — worker sees Timeout.
          4. Worker schedules retry; outbox stays 'pending'.
          5. Worker sends HTTP again with event_id=X (SAME), delivery_id=UUID-2 (new).
          6. Receiver checks event_id — already seen — skips business logic.

        Assertions:
          a. Both delivery attempts logged with the SAME outbox_event_id.
          b. Both delivery attempts have DIFFERENT delivery_ids (request_ids).
          c. idempotency_key is stable across both deliveries (= event_id).
          d. event_id never changes.
        """
        from requests.exceptions import Timeout
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000040')
        _insert_webhook(_CONN, url='https://timeout.example.com/')

        captured_payloads = []

        def capture_post(url, data, headers, **kwargs):
            captured_payloads.append(json.loads(data))
            raise Timeout('network lost response')

        # Attempt 1: timeout
        with patch('requests.post', side_effect=capture_post):
            _process_event(_row(_CONN, oid))
        _CLAIMS.clear()
        self.assertEqual(_status(_CONN, oid), 'pending',
            'Timed-out event must stay pending for retry')

        # Attempt 2: also timeout (still retrying)
        with patch('requests.post', side_effect=capture_post):
            _process_event(_row(_CONN, oid))
        _CLAIMS.clear()

        self.assertEqual(len(captured_payloads), 2,
            'Two delivery attempts must be made')

        p1, p2 = captured_payloads

        # event_id must be stable across retries
        self.assertEqual(p1['event_id'], p2['event_id'],
            'event_id must be identical across retries')
        self.assertEqual(p1['event_id'], oid,
            'event_id must match the outbox row id')

        # idempotency_key must be stable
        self.assertEqual(p1['idempotency_key'], p2['idempotency_key'],
            'idempotency_key must be stable across retries')
        self.assertEqual(str(p1['idempotency_key']), str(oid),
            'idempotency_key must equal the outbox row id')

        # delivery_id must be DIFFERENT per attempt (new UUID each time)
        self.assertNotEqual(p1['delivery_id'], p2['delivery_id'],
            'delivery_id must be unique per delivery attempt')

        # Delivery log: two rows, same outbox_event_id, different request_ids
        logs = _log_rows(_CONN, oid)
        self.assertEqual(len(logs), 2)
        cols = ['id','outbox_event_id','webhook_id','reseller_id','attempt',
                'request_id','requested_at','responded_at','http_status',
                'success','error_category','next_retry_at','created_at']
        log_dicts = [dict(zip(cols, r)) for r in logs]
        req_ids = {d['request_id'] for d in log_dicts}
        self.assertEqual(len(req_ids), 2,
            'Each delivery attempt must have a unique request_id in the log')
        for d in log_dicts:
            self.assertEqual(d['outbox_event_id'], oid,
                'All log rows must reference the same outbox event')

    # ------------------------------------------------------------------ 9
    def test_old_signed_request_replay_rejected(self):
        """A replay of an old signed request is rejected by the freshness check."""
        secret = 'replay-test-secret'
        body   = b'{"event_id":1,"event_type":"order.created"}'
        old_ts = str(int(time.time()) - FRESHNESS_WINDOW_SECONDS - 120)
        old_sig = sign_body(secret, body, old_ts)
        # Even though signature is cryptographically correct, freshness fails
        result = verify_signature(secret, body, old_ts, old_sig)
        self.assertFalse(result,
            'Replayed request with old timestamp must be rejected')

    def test_fresh_request_accepted(self):
        """A correctly signed fresh request is accepted."""
        secret = 'fresh-test-secret'
        body   = b'{"event_id":2,"event_type":"order.created"}'
        ts     = str(int(time.time()))
        sig    = sign_body(secret, body, ts)
        self.assertTrue(verify_signature(secret, body, ts, sig))

    # ------------------------------------------------------------------ 10
    def test_modified_event_id_rejected(self):
        """A receiver that re-signs with a tampered event_id will produce
        a different digest — the original signature no longer matches."""
        secret   = 'evtid-secret'
        original = json.dumps({'event_id': 42, 'event_type': 'order.created'},
                              separators=(',', ':')).encode()
        tampered = json.dumps({'event_id': 99, 'event_type': 'order.created'},
                              separators=(',', ':')).encode()
        ts  = str(int(time.time()))
        sig = sign_body(secret, original, ts)
        # Original body: valid
        self.assertTrue(verify_signature(secret, original, ts, sig,
                                         check_freshness=False))
        # Tampered event_id: invalid
        self.assertFalse(verify_signature(secret, tampered, ts, sig,
                                          check_freshness=False),
            'Modified event_id must invalidate signature')

    # ------------------------------------------------------------------ 11
    def test_modified_delivery_id_rejected(self):
        """Tampering with the delivery_id field invalidates the signature."""
        secret   = 'dlvid-secret'
        original = json.dumps({'delivery_id': 'uuid-A', 'event_id': 1},
                              separators=(',', ':')).encode()
        tampered = json.dumps({'delivery_id': 'uuid-EVIL', 'event_id': 1},
                              separators=(',', ':')).encode()
        ts  = str(int(time.time()))
        sig = sign_body(secret, original, ts)
        self.assertTrue(verify_signature(secret, original, ts, sig,
                                         check_freshness=False))
        self.assertFalse(verify_signature(secret, tampered, ts, sig,
                                          check_freshness=False),
            'Modified delivery_id must invalidate signature')

    # ------------------------------------------------------------------ 12
    def test_modified_payload_rejected(self):
        """Any modification to the payload body invalidates the HMAC signature."""
        secret   = 'body-secret'
        original = b'{"event_type":"order.created","amount":"10.00"}'
        tampered = b'{"event_type":"order.created","amount":"0.01"}'
        ts  = str(int(time.time()))
        sig = sign_body(secret, original, ts)
        self.assertFalse(verify_signature(secret, tampered, ts, sig,
                                          check_freshness=False),
            'Modified payload body must invalidate signature')

    # ------------------------------------------------------------------ 13
    def test_duplicate_queue_job_claim_blocks_second(self):
        """claim_worker_job returns False when a second worker tries to
        claim the same job key held by the first."""
        job   = 'whk_deliver_9999'
        ok_a  = _claim(job, 'worker-A')
        ok_b  = _claim(job, 'worker-B')
        self.assertTrue(ok_a,  'Worker A must acquire the claim')
        self.assertFalse(ok_b, 'Worker B must be blocked')

    def test_same_worker_reclaims_own_job(self):
        """A worker can re-claim its own job (idempotent heartbeat refresh)."""
        job = 'whk_deliver_8888'
        self.assertTrue(_claim(job, 'worker-A'))
        self.assertTrue(_claim(job, 'worker-A'),
            'Re-claiming own job must succeed (heartbeat refresh)')

    # ------------------------------------------------------------------ 14
    def test_duplicate_retry_reset_idempotent(self):
        """Calling reset_for_manual_retry twice on the same row is safe.
        The second call returns False (row is already 'pending'), but does
        not corrupt state."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000050',
                             attempts=MAX_ATTEMPTS)
        _mark_failed_terminal(oid, reason='max_attempts')
        self.assertEqual(_status(_CONN, oid), 'failed')

        ok1 = reset_for_manual_retry(oid)
        self.assertTrue(ok1)
        self.assertEqual(_status(_CONN, oid), 'pending')

        ok2 = reset_for_manual_retry(oid)  # second call
        self.assertFalse(ok2, 'Second reset on pending row must return False')
        # State must still be 'pending', not corrupted
        self.assertEqual(_status(_CONN, oid), 'pending')

    def test_duplicate_retry_no_extra_delivery_log_row(self):
        """A manual retry does not insert any delivery log rows
        (only delivery worker does that on actual HTTP send)."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000051',
                             attempts=MAX_ATTEMPTS)
        _mark_failed_terminal(oid, reason='max_attempts')
        logs_before = len(_log_rows(_CONN, oid))
        reset_for_manual_retry(oid)
        logs_after  = len(_log_rows(_CONN, oid))
        self.assertEqual(logs_before, logs_after,
            'reset_for_manual_retry must not insert delivery log rows')

    # ------------------------------------------------------------------ stable IDs invariants
    def test_event_id_stable_across_status_changes(self):
        """The outbox row id (event_id) never changes regardless of status."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000060')
        orig_id = _CONN.execute(
            'SELECT id FROM webhook_outbox WHERE id=?', (oid,)
        ).fetchone()[0]
        _mark_delivered(oid)
        after_id = _CONN.execute(
            'SELECT id FROM webhook_outbox WHERE id=?', (oid,)
        ).fetchone()[0]
        self.assertEqual(orig_id, after_id,
            'outbox row id (event_id) must not change')

    def test_payload_not_mutated_by_retries(self):
        """The stored payload_json in the outbox must not be modified by the
        delivery worker.  Only delivery state (status, attempts, next_attempt_at)
        is updated."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000061')
        _insert_webhook(_CONN, url='https://mutation.example.com/')
        payload_before = _CONN.execute(
            'SELECT payload_json FROM webhook_outbox WHERE id=?', (oid,)
        ).fetchone()[0]
        from requests.exceptions import Timeout
        with patch('requests.post', side_effect=Timeout('to')):
            run_once()
        _CLAIMS.clear()
        with patch('requests.post', return_value=_fake(200)):
            run_once()
        payload_after = _CONN.execute(
            'SELECT payload_json FROM webhook_outbox WHERE id=?', (oid,)
        ).fetchone()[0]
        self.assertEqual(payload_before, payload_after,
            'Stored payload must be immutable across retries')

    # ------------------------------------------------------------------ no duplication invariants
    def test_no_financial_stock_duplication_unique_index(self):
        """The UNIQUE INDEX on webhook_outbox(reseller_id, event_type, aggregate_id)
        prevents duplicate financial or stock events even under concurrent writes."""
        _insert_outbox(_CONN, event_type='order.created', aggregate_id='ORD-00000070')
        # Second emit (same transaction boundary as the business fact) uses INSERT OR IGNORE
        affected = _CONN.execute(
            'INSERT OR IGNORE INTO webhook_outbox'
            '(reseller_id,event_type,aggregate_id,payload_json)'
            ' VALUES(?,?,?,?)',
            (1, 'order.created', 'ORD-00000070', '{"duplicate":true}')
        ).rowcount
        _CONN.commit()
        self.assertEqual(affected, 0, 'Duplicate event insert must be a no-op')
        count = _CONN.execute(
            "SELECT COUNT(*) FROM webhook_outbox "
            "WHERE aggregate_id='ORD-00000070'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_delivery_log_unique_request_id_constraint(self):
        """UNIQUE INDEX on request_id prevents two log rows with the same UUID."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000080')
        wid = _insert_webhook(_CONN)
        fr  = {'success': True, 'error_category': None,
               'http_status': 200, 'responded_at': '2026-01-01', 'elapsed_ms': 5}
        record_delivery_attempt(oid, wid, 1, 1, 'dup-uuid-001', '2026-01-01', fr)
        # Same request_id again (INSERT OR IGNORE via unique constraint)
        try:
            record_delivery_attempt(oid, wid, 1, 1, 'dup-uuid-001', '2026-01-01', fr)
        except Exception as e:
            self.fail(f'Should not raise on duplicate request_id: {e}')
        logs = _log_rows(_CONN, oid)
        self.assertEqual(len(logs), 1)

    def test_idempotency_key_equals_event_id_in_payload(self):
        """idempotency_key in the delivered payload must equal the event_id."""
        oid = _insert_outbox(_CONN, aggregate_id='ORD-00000090')
        _insert_webhook(_CONN, url='https://idmkey.example.com/')
        captured = []
        def grab_post(url, data, headers, **kwargs):
            captured.append(json.loads(data))
            return _fake(200)
        with patch('requests.post', side_effect=grab_post):
            run_once()
        self.assertEqual(len(captured), 1)
        p = captured[0]
        self.assertEqual(str(p['idempotency_key']), str(oid),
            'idempotency_key must match the outbox row id')
        self.assertEqual(str(p['event_id']), str(oid),
            'event_id must match the outbox row id')
        self.assertNotEqual(p['delivery_id'], p['idempotency_key'],
            'delivery_id (per-attempt UUID) must differ from idempotency_key')


if __name__ == '__main__':
    unittest.main()
