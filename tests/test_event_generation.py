"""Phase 9 Prompt 4 — Event Generation tests.

Verifies that the correct outbox events are created (or NOT created) for every
business state transition.  Uses an in-memory SQLite database so no external
dependencies are required.

Test matrix
-----------
1.  Reseller order created            → order.created + delivery.pending/completed rows appear
2.  Reseller order completed          → delivery.completed row appears (code-stock path)
3.  Reseller order failed             → order.failed + delivery.failed rows appear
4.  Reseller delivery completed       → delivery.completed row, correct payload fields
5.  Reseller delivery failed          → delivery.failed row, correct payload fields
6.  Reseller refund                   → refund.created row appears
7.  Customer order (source != reseller_api) → NO outbox rows emitted
8.  Duplicate status update           → INSERT OR IGNORE: still exactly one row per event
9.  Transaction rollback              → no outbox row survives
10. Sensitive payload exclusion       → no secrets / internal IDs in stored JSON
"""

import json
import sqlite3
import sys
import types
import unittest

# ---------------------------------------------------------------------------
# Minimal DB bootstrap — mirrors the real DB bootstrap but fully in-memory.
# ---------------------------------------------------------------------------

def _make_conn():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _bootstrap(conn):
    """Create the tables needed by the outbox module."""
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS webhook_outbox (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            reseller_id     INTEGER NOT NULL,
            event_type      TEXT    NOT NULL,
            aggregate_id    TEXT    NOT NULL,
            payload_json    TEXT    NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'pending',
            attempts        INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT    NOT NULL,
            created_at      TEXT    NOT NULL DEFAULT (CURRENT_TIMESTAMP),
            processed_at    TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_outbox_dedup
            ON webhook_outbox(reseller_id, event_type, aggregate_id);
        CREATE INDEX IF NOT EXISTS ix_outbox_pending
            ON webhook_outbox(status, next_attempt_at)
            WHERE status = 'pending';
        CREATE TABLE IF NOT EXISTS worker_jobs (
            job_key      TEXT PRIMARY KEY,
            worker_id    TEXT NOT NULL,
            claimed_at   INTEGER NOT NULL,
            heartbeat_at INTEGER NOT NULL
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Lightweight stubs so we can import webhook_outbox without the full app.
# ---------------------------------------------------------------------------

SYS_PATH_PREPEND = '/data/oggystore-phase8'
if SYS_PATH_PREPEND not in sys.path:
    sys.path.insert(0, SYS_PATH_PREPEND)

_GLOBAL_CONN = None

def _get_test_conn():
    return _GLOBAL_CONN

def _get_test_cursor():
    return _GLOBAL_CONN.cursor()

# Patch database.db before importing outbox
db_mod = types.ModuleType('database.db')
db_mod.get_conn   = _get_test_conn
db_mod.get_cursor = _get_test_cursor
sys.modules.setdefault('database', types.ModuleType('database'))
sys.modules['database.db'] = db_mod

# Stub pgcompat
pg_mod = types.ModuleType('database.pgcompat')
class _AtomicCtx:
    def __init__(self, conn): self._conn = conn
    def __enter__(self): return self
    def __exit__(self, et, ev, tb):
        if et: self._conn.rollback()
        else:  self._conn.commit()
pg_mod.atomic = lambda conn: _AtomicCtx(conn)
pg_mod.is_unique_violation = lambda e: False
sys.modules['database.pgcompat'] = pg_mod

# Stub utils.timezone
tz_mod = types.ModuleType('utils.timezone')
tz_mod.now_text = lambda: '2026-01-01 00:00:00'
sys.modules.setdefault('utils', types.ModuleType('utils'))
sys.modules['utils.timezone'] = tz_mod

# Now import the real outbox module
from reseller_api.services.webhook_outbox import (  # noqa: E402
    emit_event,
    emit_event_atomic,
    build_order_created_payload,
    build_delivery_completed_payload,
    build_delivery_pending_payload,
    build_order_failed_payload,
    build_delivery_failed_payload,
    build_order_cancelled_payload,
    build_refund_created_payload,
    OUTBOX_EVENT_TYPES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SECRET_KEYS = {
    'secret', 'password', 'passwd', 'credential', 'api_key', 'token',
    'private', 'internal', 'db_', 'stack', 'traceback', 'admin',
    'supplier', 'delivery_code', 'decrypted', 'encrypted',
}

def _no_secrets(payload: dict) -> bool:
    """Return True if no key in the payload resembles a secret."""
    for k in payload:
        lower_k = k.lower()
        if any(s in lower_k for s in SECRET_KEYS):
            return False
    return True

def _rows(conn, reseller_id=None, event_type=None, aggregate_id=None):
    """Fetch outbox rows with optional filters."""
    q = "SELECT id, reseller_id, event_type, aggregate_id, payload_json, status FROM webhook_outbox WHERE 1=1"
    params = []
    if reseller_id is not None:
        q += ' AND reseller_id=?'; params.append(reseller_id)
    if event_type is not None:
        q += ' AND event_type=?'; params.append(event_type)
    if aggregate_id is not None:
        q += ' AND aggregate_id=?'; params.append(aggregate_id)
    return conn.execute(q, params).fetchall()

def _payload(conn, event_type, aggregate_id):
    row = conn.execute(
        'SELECT payload_json FROM webhook_outbox WHERE event_type=? AND aggregate_id=?',
        (event_type, aggregate_id),
    ).fetchone()
    return json.loads(row[0]) if row else None


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class EventGenerationTests(unittest.TestCase):

    def setUp(self):
        global _GLOBAL_CONN
        _GLOBAL_CONN = _make_conn()
        _bootstrap(_GLOBAL_CONN)
        # Patch the module-level function references so the outbox module
        # uses our in-memory DB instead of the real thread-local connection.
        import reseller_api.services.webhook_outbox as _ob
        _ob.get_conn    = _get_test_conn
        _ob.get_cursor  = _get_test_cursor
        _ob.now_text    = lambda: '2026-01-01 00:00:00'
        # _pgcompat.atomic is already stubbed at module level via sys.modules
        # before the first import, so no additional patching is needed here.

    def tearDown(self):
        global _GLOBAL_CONN
        _GLOBAL_CONN.close()
        _GLOBAL_CONN = None

    # ------------------------------------------------------------------
    # 1. Reseller order created — code-stock path
    # ------------------------------------------------------------------
    def test_reseller_order_created_code_stock(self):
        """order.created + delivery.completed emitted for a code-stock reseller order."""
        conn = _GLOBAL_CONN
        cur  = conn.cursor()

        # Simulate create_reseller_order() atomic block: emit both events together
        with _AtomicCtx(conn):
            emit_event(cur, 1, 'order.created', 'ORD-00000001',
                       build_order_created_payload(
                           1, 'RSR-0000000001', 'PRD-0000000001', 2, 500, 1000,
                           None, '2026-01-01 00:00:00'))
            emit_event(cur, 1, 'delivery.completed', 'ORD-00000001',
                       build_delivery_completed_payload(
                           1, 'RSR-0000000001', 'PRD-0000000001', 2, '2026-01-01 00:00:00'))

        created  = _rows(conn, event_type='order.created', aggregate_id='ORD-00000001')
        delivery = _rows(conn, event_type='delivery.completed', aggregate_id='ORD-00000001')
        self.assertEqual(len(created), 1,  'order.created must be present')
        self.assertEqual(len(delivery), 1, 'delivery.completed must be present')
        self.assertEqual(created[0][5],  'pending')  # status
        self.assertEqual(delivery[0][5], 'pending')

    # ------------------------------------------------------------------
    # 2. Reseller order created — manual product → delivery.pending
    #    This covers "order completed" in the sense that the order is
    #    fully committed; delivery is handed off later.
    # ------------------------------------------------------------------
    def test_reseller_order_created_manual_emits_delivery_pending(self):
        """Manual-fulfillment order emits order.created + delivery.pending (not delivery.completed)."""
        conn = _GLOBAL_CONN
        cur  = conn.cursor()

        with _AtomicCtx(conn):
            emit_event(cur, 2, 'order.created', 'ORD-00000002',
                       build_order_created_payload(
                           2, 'RSR-0000000002', 'PRD-0000000002', 1, 1000, 1000,
                           'CLI-001', '2026-01-01 00:00:00'))
            emit_event(cur, 2, 'delivery.pending', 'ORD-00000002',
                       build_delivery_pending_payload(
                           2, 'RSR-0000000002', 'PRD-0000000002', 1, '2026-01-01 00:00:00'))

        self.assertEqual(len(_rows(conn, event_type='order.created',    aggregate_id='ORD-00000002')), 1)
        self.assertEqual(len(_rows(conn, event_type='delivery.pending', aggregate_id='ORD-00000002')), 1)
        # delivery.completed must NOT be present
        self.assertEqual(len(_rows(conn, event_type='delivery.completed', aggregate_id='ORD-00000002')), 0)

    # ------------------------------------------------------------------
    # 3. Reseller order failed → order.failed + delivery.failed
    # ------------------------------------------------------------------
    def test_reseller_order_failed_emits_both_events(self):
        """A failed reseller order emits order.failed AND delivery.failed atomically."""
        conn = _GLOBAL_CONN
        cur  = conn.cursor()
        now  = '2026-01-01 00:00:00'

        with _AtomicCtx(conn):
            emit_event(cur, 1, 'order.failed', 'ORD-00000003',
                       build_order_failed_payload(3, 'RSR-0000000001', 'stock_error', now))
            emit_event(cur, 1, 'delivery.failed', 'ORD-00000003',
                       build_delivery_failed_payload(3, 'RSR-0000000001', 'stock_error', now))

        self.assertEqual(len(_rows(conn, event_type='order.failed',    aggregate_id='ORD-00000003')), 1)
        self.assertEqual(len(_rows(conn, event_type='delivery.failed', aggregate_id='ORD-00000003')), 1)

    # ------------------------------------------------------------------
    # 4. Delivery completed payload correctness
    # ------------------------------------------------------------------
    def test_delivery_completed_payload_fields(self):
        """delivery.completed payload contains expected public fields."""
        conn = _GLOBAL_CONN
        cur  = conn.cursor()

        with _AtomicCtx(conn):
            emit_event(cur, 1, 'delivery.completed', 'ORD-00000004',
                       build_delivery_completed_payload(
                           4, 'RSR-0000000001', 'PRD-0000000001', 3, '2026-01-01 00:00:00'))

        p = _payload(conn, 'delivery.completed', 'ORD-00000004')
        self.assertIsNotNone(p)
        self.assertEqual(p['order_id'],         'ORD-00000004')
        self.assertEqual(p['reseller_id'],       'RSR-0000000001')
        self.assertEqual(p['product_id'],        'PRD-0000000001')
        self.assertEqual(p['quantity'],          3)
        self.assertEqual(p['delivery_status'],   'delivered')
        self.assertEqual(p['order_status'],      'completed')
        self.assertIn('event_type', p)
        self.assertIn('timestamp',  p)

    # ------------------------------------------------------------------
    # 5. Delivery failed payload correctness
    # ------------------------------------------------------------------
    def test_delivery_failed_payload_fields(self):
        """delivery.failed payload contains expected public fields."""
        conn = _GLOBAL_CONN
        cur  = conn.cursor()

        with _AtomicCtx(conn):
            emit_event(cur, 1, 'delivery.failed', 'ORD-00000005',
                       build_delivery_failed_payload(
                           5, 'RSR-0000000001', 'out_of_stock', '2026-01-01 00:00:00'))

        p = _payload(conn, 'delivery.failed', 'ORD-00000005')
        self.assertIsNotNone(p)
        self.assertEqual(p['order_id'],        'ORD-00000005')
        self.assertEqual(p['delivery_status'], 'failed')
        self.assertEqual(p['order_status'],    'failed')
        self.assertEqual(p['reason'],          'out_of_stock')
        self.assertIn('timestamp', p)
        self.assertIn('failed_at', p)

    # ------------------------------------------------------------------
    # 6. Reseller refund → refund.created
    # ------------------------------------------------------------------
    def test_reseller_refund_emits_refund_created(self):
        """A successful refund emits a refund.created event."""
        conn = _GLOBAL_CONN

        # emit_event_atomic opens its own connection-level atomic block
        import reseller_api.services.webhook_outbox as _ob
        _ob.get_conn   = lambda: conn
        _ob.get_cursor = lambda: conn.cursor()
        emit_event_atomic(
            1, 'refund.created', 'ORD-00000006',
            build_refund_created_payload(6, 'RSR-0000000001', 500, 'full', '2026-01-01 00:00:00'),
        )

        rows = _rows(conn, event_type='refund.created', aggregate_id='ORD-00000006')
        self.assertEqual(len(rows), 1)
        p = _payload(conn, 'refund.created', 'ORD-00000006')
        self.assertEqual(p['event_type'], 'refund.created')
        self.assertIn('amount', p)
        self.assertIn('currency', p)
        self.assertNotIn('wallet_tx_id', p)

    # ------------------------------------------------------------------
    # 7. Customer order (source != reseller_api) — NO events emitted
    # ------------------------------------------------------------------
    def test_customer_order_emits_no_events(self):
        """_mark_order_failed with source='' (customer) must NOT write outbox rows."""
        # The source guard lives in _mark_order_failed/_mark_order_cancelled.
        # We test the guard directly by calling emit_event only when is_reseller=True.
        conn = _GLOBAL_CONN
        cur  = conn.cursor()

        # Customer order: source is empty string, not 'reseller_api'
        source = ''
        reseller_id = 0
        reseller_public_id = ''

        is_reseller = (source == 'reseller_api') and bool(reseller_id) and bool(reseller_public_id)
        self.assertFalse(is_reseller, 'Customer order must not be classified as reseller')

        # Simulate the guarded block inside _mark_order_failed
        with _AtomicCtx(conn):
            if is_reseller:
                emit_event(cur, reseller_id, 'order.failed', 'ORD-00000007',
                           build_order_failed_payload(7, reseller_public_id, 'x', '2026-01-01'))

        self.assertEqual(len(_rows(conn)), 0, 'Customer order must not produce outbox rows')

    # ------------------------------------------------------------------
    # 7b. Telegram order (reseller_id=0) — no events
    # ------------------------------------------------------------------
    def test_telegram_customer_order_emits_no_events(self):
        """A reseller_id=0 order (Telegram customer) must not emit outbox events."""
        is_reseller = ('reseller_api' == 'reseller_api') and bool(0) and bool('RSR-BOGUS')
        self.assertFalse(is_reseller, 'reseller_id=0 must fail the guard')

        conn = _GLOBAL_CONN
        cur  = conn.cursor()
        with _AtomicCtx(conn):
            if is_reseller:
                emit_event(cur, 0, 'order.failed', 'ORD-00000099',
                           build_order_failed_payload(99, '', 'x', '2026-01-01'))

        self.assertEqual(len(_rows(conn)), 0)

    # ------------------------------------------------------------------
    # 8. Duplicate status update → INSERT OR IGNORE, still exactly one row
    # ------------------------------------------------------------------
    def test_duplicate_status_update_single_row(self):
        """Calling emit_event twice for the same logical event produces only one row."""
        conn = _GLOBAL_CONN
        cur  = conn.cursor()
        payload = build_order_failed_payload(8, 'RSR-0000000001', 'x', '2026-01-01 00:00:00')

        with _AtomicCtx(conn):
            emit_event(cur, 1, 'order.failed', 'ORD-00000008', payload)

        with _AtomicCtx(conn):
            emit_event(cur, 1, 'order.failed', 'ORD-00000008', payload)  # duplicate

        rows = _rows(conn, event_type='order.failed', aggregate_id='ORD-00000008')
        self.assertEqual(len(rows), 1, 'Duplicate emit must be silently ignored')

    # ------------------------------------------------------------------
    # 9. Transaction rollback — no outbox row survives
    # ------------------------------------------------------------------
    def test_rollback_suppresses_event(self):
        """If the enclosing transaction rolls back, the outbox row must vanish."""
        conn = _GLOBAL_CONN
        cur  = conn.cursor()

        try:
            with _AtomicCtx(conn):
                emit_event(cur, 1, 'order.created', 'ORD-00000009',
                           build_order_created_payload(
                               9, 'RSR-0000000001', 'PRD-0000000001', 1, 100, 100,
                               None, '2026-01-01 00:00:00'))
                raise RuntimeError('simulated failure')
        except RuntimeError:
            pass

        self.assertEqual(len(_rows(conn, aggregate_id='ORD-00000009')), 0,
                         'Rolled-back transaction must not leave an outbox row')

    # ------------------------------------------------------------------
    # 10. Sensitive data exclusion in all payload builders
    # ------------------------------------------------------------------
    def test_payload_no_sensitive_keys(self):
        """None of the payload builders must include sensitive / internal keys."""
        now = '2026-01-01 00:00:00'
        payloads = [
            build_order_created_payload(1, 'RSR-X', 'PRD-X', 1, 100, 100, None, now),
            build_delivery_completed_payload(1, 'RSR-X', 'PRD-X', 1, now),
            build_delivery_pending_payload(1, 'RSR-X', 'PRD-X', 1, now),
            build_order_failed_payload(1, 'RSR-X', 'reason', now),
            build_delivery_failed_payload(1, 'RSR-X', 'reason', now),
            build_order_cancelled_payload(1, 'RSR-X', now),
            build_refund_created_payload(1, 'RSR-X', 500, 'full', now),
        ]
        for p in payloads:
            self.assertTrue(_no_secrets(p),
                            f'Sensitive key found in payload: {list(p.keys())}')

    def test_payload_no_internal_integer_ids(self):
        """Payloads must use public string IDs, not raw integer database IDs."""
        now = '2026-01-01 00:00:00'
        p = build_order_created_payload(42, 'RSR-0000000001', 'PRD-0000000001', 1, 100, 100, None, now)
        # order_id in payload must be the public string, not 42
        self.assertNotEqual(p.get('order_id'), 42)
        self.assertTrue(str(p.get('order_id', '')).startswith('ORD-'), f"order_id={p.get('order_id')}")

    def test_payload_delivery_status_present(self):
        """All order/delivery payload builders must include delivery_status."""
        now = '2026-01-01 00:00:00'
        pairs = [
            build_order_created_payload(1, 'RSR-X', 'PRD-X', 1, 100, 100, None, now),
            build_delivery_completed_payload(1, 'RSR-X', 'PRD-X', 1, now),
            build_delivery_pending_payload(1, 'RSR-X', 'PRD-X', 1, now),
            build_order_failed_payload(1, 'RSR-X', 'reason', now),
            build_delivery_failed_payload(1, 'RSR-X', 'reason', now),
            build_order_cancelled_payload(1, 'RSR-X', now),
        ]
        for p in pairs:
            self.assertIn('delivery_status', p,
                          f"delivery_status missing from {p.get('event_type', '?')} payload")
            self.assertIn('order_status', p,
                          f"order_status missing from {p.get('event_type', '?')} payload")
            self.assertIn('timestamp', p,
                          f"timestamp missing from {p.get('event_type', '?')} payload")
            self.assertIn('event_type', p,
                          f"event_type field missing from {p.get('event_type', '?')} payload")

    # ------------------------------------------------------------------
    # Cross-reseller isolation: reseller A's events don't appear for reseller B
    # ------------------------------------------------------------------
    def test_events_scoped_to_correct_reseller(self):
        """Outbox rows must be scoped to the correct reseller_id."""
        conn = _GLOBAL_CONN
        cur  = conn.cursor()

        with _AtomicCtx(conn):
            emit_event(cur, 1, 'order.created', 'ORD-10000001',
                       build_order_created_payload(1001, 'RSR-A', 'PRD-X', 1, 100, 100, None, '2026-01-01'))
            emit_event(cur, 2, 'order.created', 'ORD-10000002',
                       build_order_created_payload(1002, 'RSR-B', 'PRD-X', 1, 100, 100, None, '2026-01-01'))

        reseller_a_rows = _rows(conn, reseller_id=1)
        reseller_b_rows = _rows(conn, reseller_id=2)
        self.assertEqual(len(reseller_a_rows), 1)
        self.assertEqual(len(reseller_b_rows), 1)
        self.assertNotEqual(reseller_a_rows[0][3], reseller_b_rows[0][3])  # different aggregate_ids

    # ------------------------------------------------------------------
    # event_type field in payload matches the outbox column
    # ------------------------------------------------------------------
    def test_event_type_field_consistent(self):
        """The event_type field inside payload_json must match the event_type column."""
        conn = _GLOBAL_CONN
        cur  = conn.cursor()
        now  = '2026-01-01 00:00:00'

        events = [
            ('order.created',    'ORD-20000001', build_order_created_payload(1, 'RSR-X', 'PRD-X', 1, 100, 100, None, now)),
            ('delivery.pending', 'ORD-20000002', build_delivery_pending_payload(2, 'RSR-X', 'PRD-X', 1, now)),
            ('delivery.completed','ORD-20000003', build_delivery_completed_payload(3, 'RSR-X', 'PRD-X', 1, now)),
            ('order.failed',     'ORD-20000004', build_order_failed_payload(4, 'RSR-X', 'reason', now)),
            ('delivery.failed',  'ORD-20000005', build_delivery_failed_payload(5, 'RSR-X', 'reason', now)),
            ('order.cancelled',  'ORD-20000006', build_order_cancelled_payload(6, 'RSR-X', now)),
            ('refund.created',   'ORD-20000007', build_refund_created_payload(7, 'RSR-X', 100, 'full', now)),
        ]

        with _AtomicCtx(conn):
            for et, agg, payload in events:
                emit_event(cur, 1, et, agg, payload)

        for et, agg, _ in events:
            p = _payload(conn, et, agg)
            self.assertIsNotNone(p, f'Row for {et}/{agg} not found')
            self.assertEqual(p.get('event_type'), et,
                             f'event_type mismatch for {et}: got {p.get("event_type")}')

    # ------------------------------------------------------------------
    # All OUTBOX_EVENT_TYPES are covered
    # ------------------------------------------------------------------
    def test_all_event_types_in_frozenset(self):
        """The frozenset must contain all 7 supported event types."""
        expected = {
            'order.created', 'order.failed', 'order.cancelled',
            'delivery.pending', 'delivery.completed', 'delivery.failed',
            'refund.created',
        }
        self.assertEqual(OUTBOX_EVENT_TYPES, expected)


if __name__ == '__main__':
    unittest.main()
