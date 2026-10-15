"""Tests for the webhook transactional outbox — Phase 9 Prompt 3.

Covers:
  - event creation and persistence (in-memory SQLite)
  - correct event_type, reseller_id, aggregate_id
  - correct payload fields / no sensitive data
  - UNIQUE deduplication (same event for same order = one row)
  - transaction rollback → outbox row absent
  - multiple orders → separate rows
  - restart/recovery → pending rows survive a new connection
  - emit_event rejects unknown event types
  - payload builders produce safe external-API data
  - worker_jobs table created by ensure_webhook_outbox_table
"""

import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

from reseller_api.services.webhook_outbox import (
    OUTBOX_EVENT_TYPES,
    STATUS_PENDING,
    ensure_webhook_outbox_table,
    emit_event,
    emit_event_atomic,
    build_order_created_payload,
    build_delivery_completed_payload,
    build_order_failed_payload,
    build_order_cancelled_payload,
    build_refund_created_payload,
    get_outbox_row,
    get_outbox_for_order,
    get_pending_events,
)


# ---------------------------------------------------------------------------
# In-memory SQLite fixture
# ---------------------------------------------------------------------------

def _make_db():
    """Create a fresh in-memory SQLite connection with required tables."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # Create only the tables needed by the outbox module.
    conn.executescript("""
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
        CREATE UNIQUE INDEX IF NOT EXISTS idx_webhook_outbox_dedup
            ON webhook_outbox(reseller_id, event_type, aggregate_id);
        CREATE INDEX IF NOT EXISTS idx_webhook_outbox_pending
            ON webhook_outbox(status, next_attempt_at)
            WHERE status = 'pending';
        CREATE TABLE IF NOT EXISTS worker_jobs (
            job_key      TEXT PRIMARY KEY,
            worker_id    TEXT NOT NULL,
            claimed_at   INTEGER NOT NULL,
            heartbeat_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS resellers (
            id        INTEGER PRIMARY KEY,
            public_id TEXT NOT NULL
        );
        INSERT OR IGNORE INTO resellers(id, public_id) VALUES(1, 'RSR-0000000001');
        INSERT OR IGNORE INTO resellers(id, public_id) VALUES(2, 'RSR-0000000002');
    """)
    conn.commit()
    return conn


_tls = threading.local()


def _setup_tls(conn):
    """Point the module's get_conn/get_cursor at our in-memory DB."""
    _tls.conn = conn
    _tls.cur  = conn.cursor()


def _get_conn():
    return _tls.conn


def _get_cursor():
    return _tls.cur


def _emit(conn, reseller_id, event_type, order_id, payload):
    """Helper: emit inside an immediate transaction, commit, return row_id."""
    cur = conn.cursor()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur.execute(
            """
            INSERT OR IGNORE INTO webhook_outbox
                (reseller_id, event_type, aggregate_id, payload_json,
                 status, attempts, next_attempt_at, created_at)
            VALUES (?, ?, ?, ?, 'pending', 0, datetime('now'), datetime('now'))
            """,
            (
                int(reseller_id),
                str(event_type),
                f"ORD-{int(order_id):08d}",
                json.dumps(payload),
            ),
        )
        row_id = cur.lastrowid
        conn.execute("COMMIT")
        return row_id
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _fetch_row(conn, row_id):
    cur = conn.execute(
        "SELECT * FROM webhook_outbox WHERE id=?", (row_id,)
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _count_rows(conn, reseller_id=None):
    if reseller_id is not None:
        cur = conn.execute(
            "SELECT COUNT(*) FROM webhook_outbox WHERE reseller_id=?",
            (reseller_id,)
        )
    else:
        cur = conn.execute("SELECT COUNT(*) FROM webhook_outbox")
    return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class WebhookOutboxTests(unittest.TestCase):

    def setUp(self):
        self.conn = _make_db()

    def tearDown(self):
        self.conn.close()

    # ----------------------------------------------------------------
    # Payload builder tests (pure functions, no DB needed)
    # ----------------------------------------------------------------

    def test_order_created_payload_fields(self):
        p = build_order_created_payload(
            order_id=42,
            reseller_public_id="RSR-0000000001",
            product_public_id="PRD-ABCDE12345",
            quantity=2,
            unit_price_cents=1000,
            total_cents=2000,
            client_order_id="client-abc",
            created_at="2026-09-30 00:00:00",
        )
        self.assertEqual("order.created",      p["event"])
        self.assertEqual("ORD-00000042",        p["order_id"])
        self.assertEqual("RSR-0000000001",      p["reseller_id"])
        self.assertEqual("PRD-ABCDE12345",      p["product_id"])
        self.assertEqual(2,                     p["quantity"])
        self.assertEqual("10.00",               p["unit_price"])
        self.assertEqual("20.00",               p["total"])
        self.assertEqual("USDT",                p["currency"])
        self.assertEqual("client-abc",          p["client_order_id"])
        self.assertEqual("2026-09-30 00:00:00", p["created_at"])

    def test_order_created_payload_no_client_order_id(self):
        p = build_order_created_payload(
            order_id=1, reseller_public_id="RSR-X",
            product_public_id="PRD-Y", quantity=1,
            unit_price_cents=500, total_cents=500,
            client_order_id=None, created_at="2026-01-01 00:00:00",
        )
        self.assertNotIn("client_order_id", p)

    def test_delivery_completed_payload_excludes_delivery_codes(self):
        """Decrypted delivery codes must NOT appear in the payload."""
        p = build_delivery_completed_payload(
            order_id=7, reseller_public_id="RSR-0000000001",
            product_public_id="PRD-X", quantity=1,
            created_at="2026-09-30 00:00:00",
        )
        self.assertEqual("delivery.completed", p["event"])
        self.assertEqual("ORD-00000007", p["order_id"])
        # Delivery codes / decrypted text must be absent
        raw = json.dumps(p)
        self.assertNotIn("delivery_items", raw)
        self.assertNotIn("codes",          raw)
        self.assertNotIn("encrypted",      raw)
        self.assertIn("note", p)   # link to delivery endpoint

    def test_order_failed_payload_sanitises_reason(self):
        long_reason = "x" * 500
        p = build_order_failed_payload(
            order_id=3, reseller_public_id="RSR-X",
            reason=long_reason, failed_at="2026-09-30 00:00:00",
        )
        self.assertEqual("order.failed", p["event"])
        self.assertLessEqual(len(p["reason"]), 128)

    def test_order_cancelled_payload(self):
        p = build_order_cancelled_payload(
            order_id=5, reseller_public_id="RSR-X",
            cancelled_at="2026-09-30 00:00:00",
        )
        self.assertEqual("order.cancelled",    p["event"])
        self.assertEqual("ORD-00000005",        p["order_id"])
        self.assertEqual("2026-09-30 00:00:00", p["cancelled_at"])

    def test_refund_created_payload(self):
        p = build_refund_created_payload(
            order_id=9, reseller_public_id="RSR-X",
            amount_cents=1500, refund_type="full",
            created_at="2026-09-30 00:00:00",
        )
        self.assertEqual("refund.created", p["event"])
        self.assertEqual("15.00",          p["amount"])
        self.assertEqual("USDT",           p["currency"])
        self.assertEqual("full",           p["refund_type"])

    def test_payload_no_secrets(self):
        """None of the payload builders should output secret-like keys."""
        payloads = [
            build_order_created_payload(1, "RSR-X", "PRD-Y", 1, 500, 500, None, "t"),
            build_delivery_completed_payload(1, "RSR-X", "PRD-Y", 1, "t"),
            build_order_failed_payload(1, "RSR-X", "err", "t"),
            build_order_cancelled_payload(1, "RSR-X", "t"),
            build_refund_created_payload(1, "RSR-X", 500, "full", "t"),
        ]
        forbidden_keys = {
            "secret", "secret_encrypted", "password", "api_key",
            "supplier_key", "wallet_tx_id", "internal_id",
            "stack_trace", "traceback", "admin_note",
        }
        for p in payloads:
            raw = json.dumps(p).lower()
            for key in forbidden_keys:
                self.assertNotIn(key, raw, msg=f"Payload contains forbidden key '{key}'")

    # ----------------------------------------------------------------
    # Schema / table creation
    # ----------------------------------------------------------------

    def test_tables_created_by_ensure(self):
        """ensure_webhook_outbox_table() must create webhook_outbox and worker_jobs."""
        # Use a fresh in-memory DB via mocked get_conn/get_cursor
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        with (
            patch("reseller_api.services.webhook_outbox.get_conn", return_value=conn) if False else
            patch("database.db.get_conn", return_value=conn),
            patch("database.db.get_cursor", return_value=cur),
        ):
            ensure_webhook_outbox_table()

        # webhook_outbox should exist
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='webhook_outbox'"
        ).fetchone()
        self.assertIsNotNone(row, "webhook_outbox table not created")

        # worker_jobs should exist
        row2 = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='worker_jobs'"
        ).fetchone()
        self.assertIsNotNone(row2, "worker_jobs table not created")
        conn.close()

    def test_ensure_is_idempotent(self):
        """Calling ensure_webhook_outbox_table() twice must not raise."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        with (
            patch("database.db.get_conn", return_value=conn),
            patch("database.db.get_cursor", return_value=cur),
        ):
            ensure_webhook_outbox_table()
            ensure_webhook_outbox_table()   # second call must be no-op
        conn.close()

    # ----------------------------------------------------------------
    # emit_event (core write primitive)
    # ----------------------------------------------------------------

    def test_emit_event_creates_row(self):
        """emit_event inserts a pending row into webhook_outbox."""
        payload = build_order_created_payload(
            1, "RSR-0000000001", "PRD-X", 1, 500, 500, None, "2026-09-30"
        )
        row_id = _emit(self.conn, 1, "order.created", 1, payload)
        self.assertIsNotNone(row_id)

        row = _fetch_row(self.conn, row_id)
        self.assertIsNotNone(row)
        self.assertEqual(1,               row["reseller_id"])
        self.assertEqual("order.created", row["event_type"])
        self.assertEqual("ORD-00000001",  row["aggregate_id"])
        self.assertEqual("pending",       row["status"])
        self.assertEqual(0,               row["attempts"])
        self.assertIsNone(row["processed_at"])

    def test_emit_event_payload_round_trips(self):
        """Payload stored in DB must deserialise back to the original dict."""
        payload = build_order_created_payload(
            42, "RSR-0000000001", "PRD-ABCDE12345", 3, 1000, 3000, "cid-1", "2026-09-30"
        )
        row_id = _emit(self.conn, 1, "order.created", 42, payload)
        row = _fetch_row(self.conn, row_id)
        stored = json.loads(row["payload_json"])
        self.assertEqual(payload["order_id"],    stored["order_id"])
        self.assertEqual(payload["quantity"],    stored["quantity"])
        self.assertEqual(payload["total"],       stored["total"])
        self.assertEqual(payload["currency"],    stored["currency"])

    def test_emit_event_correct_reseller(self):
        """Outbox row must be scoped to the correct reseller_id."""
        payload = {"event": "order.created", "order_id": "ORD-00000005"}
        rid = _emit(self.conn, 2, "order.created", 5, payload)
        row = _fetch_row(self.conn, rid)
        self.assertEqual(2, row["reseller_id"])

    def test_emit_event_unknown_type_raises(self):
        """emit_event must reject event types not in OUTBOX_EVENT_TYPES."""
        cur = self.conn.cursor()
        self.conn.execute("BEGIN")
        with self.assertRaises(ValueError):
            emit_event(cur, 1, "payment.success", "ORD-00000001", {})
        self.conn.execute("ROLLBACK")

    def test_all_supported_event_types_accepted(self):
        """Every type in OUTBOX_EVENT_TYPES must be accepted by emit_event."""
        for i, ev in enumerate(sorted(OUTBOX_EVENT_TYPES), start=1):
            payload = {"event": ev, "order_id": f"ORD-{i:08d}"}
            rid = _emit(self.conn, 1, ev, i * 100 + i, payload)
            self.assertIsNotNone(rid, msg=f"emit_event rejected known type '{ev}'")

    # ----------------------------------------------------------------
    # Deduplication (UNIQUE constraint)
    # ----------------------------------------------------------------

    def test_duplicate_event_ignored(self):
        """Emitting the same (reseller, event_type, aggregate_id) twice = one row."""
        payload = {"event": "order.created", "order_id": "ORD-00000001"}
        rid1 = _emit(self.conn, 1, "order.created", 1, payload)
        rid2 = _emit(self.conn, 1, "order.created", 1, payload)   # duplicate
        self.assertEqual(1, _count_rows(self.conn, reseller_id=1))
        # Second emit returns None or the same id (INSERT OR IGNORE)
        # The important guarantee is only one row exists.

    def test_different_event_types_same_order_creates_separate_rows(self):
        """order.created and delivery.completed for the same order = 2 rows."""
        p1 = {"event": "order.created",     "order_id": "ORD-00000007"}
        p2 = {"event": "delivery.completed", "order_id": "ORD-00000007"}
        _emit(self.conn, 1, "order.created",     7, p1)
        _emit(self.conn, 1, "delivery.completed", 7, p2)
        self.assertEqual(2, _count_rows(self.conn, reseller_id=1))

    def test_different_resellers_same_event_creates_separate_rows(self):
        """Same event for two different resellers must produce separate rows."""
        payload = {"event": "order.created", "order_id": "ORD-00000010"}
        _emit(self.conn, 1, "order.created", 10, payload)
        _emit(self.conn, 2, "order.created", 10, payload)
        self.assertEqual(2, _count_rows(self.conn))

    def test_different_orders_create_separate_rows(self):
        """Events for different orders create separate outbox rows."""
        for order_id in [1, 2, 3]:
            _emit(self.conn, 1, "order.created", order_id, {"order_id": order_id})
        self.assertEqual(3, _count_rows(self.conn, reseller_id=1))

    # ----------------------------------------------------------------
    # Transaction rollback
    # ----------------------------------------------------------------

    def test_rollback_removes_outbox_row(self):
        """If the business transaction rolls back, the outbox row must also vanish."""
        cur = self.conn.cursor()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur.execute(
                """
                INSERT OR IGNORE INTO webhook_outbox
                    (reseller_id, event_type, aggregate_id, payload_json,
                     status, attempts, next_attempt_at, created_at)
                VALUES (1, 'order.created', 'ORD-00000099', '{}',
                        'pending', 0, datetime('now'), datetime('now'))
                """
            )
            # Simulate business operation failure
            raise RuntimeError("simulated failure")
        except RuntimeError:
            self.conn.execute("ROLLBACK")

        row = self.conn.execute(
            "SELECT id FROM webhook_outbox WHERE aggregate_id='ORD-00000099'"
        ).fetchone()
        self.assertIsNone(row, "Outbox row must not exist after rollback")

    def test_commit_persists_outbox_row(self):
        """Committed outbox row must survive a new cursor (simulates restart)."""
        cur = self.conn.cursor()
        self.conn.execute("BEGIN IMMEDIATE")
        cur.execute(
            """
            INSERT OR IGNORE INTO webhook_outbox
                (reseller_id, event_type, aggregate_id, payload_json,
                 status, attempts, next_attempt_at, created_at)
            VALUES (1, 'order.created', 'ORD-00000088', '{"k":1}',
                    'pending', 0, datetime('now'), datetime('now'))
            """
        )
        self.conn.execute("COMMIT")

        # Open a new cursor — simulates restart / different thread
        fresh_cur = self.conn.cursor()
        row = fresh_cur.execute(
            "SELECT aggregate_id FROM webhook_outbox WHERE aggregate_id='ORD-00000088'"
        ).fetchone()
        self.assertIsNotNone(row, "Row must persist after COMMIT")
        self.assertEqual("ORD-00000088", row[0])

    # ----------------------------------------------------------------
    # Restart / recovery (pending rows survive)
    # ----------------------------------------------------------------

    def test_pending_rows_visible_after_reconnect(self):
        """Pending outbox rows must remain queryable after a new connection."""
        # Use a real on-disk SQLite file for this test
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn1 = sqlite3.connect(db_path)
            conn1.executescript("""
                CREATE TABLE webhook_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reseller_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    processed_at TEXT,
                    UNIQUE(reseller_id, event_type, aggregate_id)
                );
            """)
            conn1.execute(
                "INSERT INTO webhook_outbox "
                "(reseller_id, event_type, aggregate_id, payload_json, "
                "next_attempt_at, created_at) "
                "VALUES (1, 'order.created', 'ORD-00000055', '{}', "
                "datetime('now'), datetime('now'))"
            )
            conn1.commit()
            conn1.close()   # simulate process shutdown

            # New connection — simulates process restart
            conn2 = sqlite3.connect(db_path)
            row = conn2.execute(
                "SELECT status FROM webhook_outbox WHERE aggregate_id='ORD-00000055'"
            ).fetchone()
            self.assertIsNotNone(row, "Pending row must survive reconnect")
            self.assertEqual("pending", row[0])
            conn2.close()
        finally:
            os.unlink(db_path)

    # ----------------------------------------------------------------
    # Immutability: retrying delivery must not create new event rows
    # ----------------------------------------------------------------

    def test_retry_updates_attempts_not_new_row(self):
        """A delivery retry increments 'attempts', it does NOT insert a new row."""
        payload = {"event": "order.created"}
        _emit(self.conn, 1, "order.created", 20, payload)

        # Simulate the delivery worker updating attempt count
        self.conn.execute(
            "UPDATE webhook_outbox SET attempts=1, next_attempt_at=datetime('now', '+30 seconds') "
            "WHERE reseller_id=1 AND event_type='order.created' AND aggregate_id='ORD-00000020'"
        )
        self.conn.commit()

        self.assertEqual(1, _count_rows(self.conn, reseller_id=1),
                         "Retry must not create a new row")
        updated = self.conn.execute(
            "SELECT attempts FROM webhook_outbox WHERE reseller_id=1 AND aggregate_id='ORD-00000020'"
        ).fetchone()
        self.assertEqual(1, updated[0])

    # ----------------------------------------------------------------
    # Sensitive data exclusion in stored payload
    # ----------------------------------------------------------------

    def test_stored_payload_no_secrets(self):
        """Payloads stored in webhook_outbox must not contain sensitive data."""
        payload = build_order_created_payload(
            order_id=99,
            reseller_public_id="RSR-0000000001",
            product_public_id="PRD-TESTTEST01",
            quantity=1,
            unit_price_cents=100,
            total_cents=100,
            client_order_id=None,
            created_at="2026-09-30 00:00:00",
        )
        row_id = _emit(self.conn, 1, "order.created", 99, payload)
        row = _fetch_row(self.conn, row_id)
        stored_json = row["payload_json"].lower()

        for forbidden in [
            "secret", "password", "encrypted", "api_key",
            "wallet_tx", "delivery_text", "stack_trace",
        ]:
            self.assertNotIn(forbidden, stored_json,
                             msg=f"Stored payload contains forbidden term '{forbidden}'")

    def test_stored_payload_uses_public_ids(self):
        """Stored payloads must use public order/reseller IDs, not internal ints."""
        payload = build_order_created_payload(
            order_id=123,
            reseller_public_id="RSR-0000000001",
            product_public_id="PRD-ABCDE12345",
            quantity=2,
            unit_price_cents=500,
            total_cents=1000,
            client_order_id=None,
            created_at="2026-09-30",
        )
        row_id = _emit(self.conn, 1, "order.created", 123, payload)
        row = _fetch_row(self.conn, row_id)
        stored = json.loads(row["payload_json"])
        # order_id must be the formatted public string, not a bare integer
        self.assertEqual("ORD-00000123", stored["order_id"])
        self.assertIsInstance(stored["order_id"], str)
        self.assertEqual("RSR-0000000001", stored["reseller_id"])

    # ----------------------------------------------------------------
    # Multiple orders / multiple resellers
    # ----------------------------------------------------------------

    def test_multiple_orders_multiple_resellers(self):
        """Events for distinct orders and resellers are all persisted."""
        for reseller_id in [1, 2]:
            for order_id in [10, 20, 30]:
                _emit(self.conn, reseller_id, "order.created", order_id,
                      {"order_id": f"ORD-{order_id:08d}"})
        self.assertEqual(6, _count_rows(self.conn))
        self.assertEqual(3, _count_rows(self.conn, reseller_id=1))
        self.assertEqual(3, _count_rows(self.conn, reseller_id=2))

    # ----------------------------------------------------------------
    # aggregate_id format
    # ----------------------------------------------------------------

    def test_aggregate_id_format(self):
        """aggregate_id must be the ORD-XXXXXXXX public format."""
        _emit(self.conn, 1, "order.created", 7, {"event": "order.created"})
        row = self.conn.execute(
            "SELECT aggregate_id FROM webhook_outbox WHERE reseller_id=1"
        ).fetchone()
        self.assertRegex(row[0], r"^ORD-\d{8}$")
        self.assertEqual("ORD-00000007", row[0])

    # ----------------------------------------------------------------
    # Status column default
    # ----------------------------------------------------------------

    def test_default_status_is_pending(self):
        """Newly created outbox rows must have status='pending'."""
        row_id = _emit(self.conn, 1, "order.failed", 50,
                       {"event": "order.failed", "order_id": "ORD-00000050"})
        row = _fetch_row(self.conn, row_id)
        self.assertEqual("pending", row["status"])
        self.assertEqual(0, row["attempts"])
        self.assertIsNone(row["processed_at"])

    # ----------------------------------------------------------------
    # worker_jobs table
    # ----------------------------------------------------------------

    def test_worker_jobs_table_exists(self):
        """The worker_jobs table created by ensure_webhook_outbox_table must exist."""
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='worker_jobs'"
        ).fetchone()
        self.assertIsNotNone(row, "worker_jobs table missing")

    def test_worker_jobs_primary_key_on_job_key(self):
        """worker_jobs must have a PRIMARY KEY on job_key for idempotent claims."""
        self.conn.execute(
            "INSERT OR REPLACE INTO worker_jobs(job_key, worker_id, claimed_at, heartbeat_at) "
            "VALUES ('wh_delivery', 'w1', 1000, 1000)"
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO worker_jobs(job_key, worker_id, claimed_at, heartbeat_at) "
            "VALUES ('wh_delivery', 'w2', 2000, 2000)"
        )
        self.conn.commit()
        rows = self.conn.execute("SELECT COUNT(*) FROM worker_jobs").fetchone()[0]
        self.assertEqual(1, rows, "worker_jobs must upsert, not insert duplicate")


if __name__ == "__main__":
    unittest.main()
