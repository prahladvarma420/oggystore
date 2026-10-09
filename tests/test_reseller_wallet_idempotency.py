"""Database-backed retry/idempotency tests for reseller wallet operations."""
import os
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_fd, _db_path = tempfile.mkstemp(prefix="oggy_wallet_idempotency_", suffix=".db")
os.close(_fd)
import config
config.DB_PATH = _db_path
import database.db as db
db.DB_PATH = _db_path
db._tls = threading.local()
db.init_db()
import database.reseller_db as rdb
from reseller_api.services.wallet import (
    WalletError,
    credit_wallet,
    debit_wallet,
    get_balance,
)


class WalletIdempotencyTests(unittest.TestCase):
    counter = 0

    def setUp(self):
        type(self).counter += 1
        n = self.counter
        uid = 990000 + n
        db.cur.execute(
            "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
            (uid, "Idempotency", f"wallet_idempotency_{n}"),
        )
        db.conn.commit()
        reseller = rdb.create_reseller(
            f"Idempotency {n}", uid, f"wallet_idempotency_{n}"
        )
        self.reseller_id = reseller["id"]
        rdb.set_reseller_status(self.reseller_id, "active")

    def ledger(self):
        return sorted(
            rdb.list_wallet_transactions(self.reseller_id, 1000),
            key=lambda row: row["id"],
        )

    def assert_reconciled(self):
        running = 0
        for row in self.ledger():
            running += int(row["amount_cents"])
            self.assertEqual(int(row["balance_after_cents"]), running)
        self.assertEqual(get_balance(self.reseller_id)["balance_cents"], running)

    def test_same_request_twice_changes_balance_once(self):
        credit_wallet(self.reseller_id, "100", "fund:same-request")
        first = debit_wallet(self.reseller_id, "20", "request:twice")
        second = debit_wallet(self.reseller_id, "20", "request:twice")
        self.assertEqual(first.status, "posted")
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(first.transaction_id, second.transaction_id)
        self.assertEqual(second.balance_cents, first.balance_cents)
        self.assertEqual(get_balance(self.reseller_id)["balance_cents"], 8000)
        self.assertEqual(sum(r["reference"] == "request:twice" for r in self.ledger()), 1)
        self.assert_reconciled()

    def test_same_reference_with_different_payload_is_conflict(self):
        credit_wallet(self.reseller_id, "100", "fund:conflict")
        original = debit_wallet(self.reseller_id, "20", "reference:reused")
        with self.assertRaises(WalletError) as caught:
            debit_wallet(self.reseller_id, "25", "reference:reused")
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        self.assertEqual(caught.exception.transaction_id, original.transaction_id)
        self.assertEqual(get_balance(self.reseller_id)["balance_cents"], 8000)
        self.assertEqual(sum(r["reference"] == "reference:reused" for r in self.ledger()), 1)
        self.assert_reconciled()

    def test_same_request_concurrently_posts_once(self):
        credit_wallet(self.reseller_id, "100", "fund:concurrent-retry")
        barrier = threading.Barrier(2)
        results = queue.Queue()

        def worker():
            try:
                barrier.wait(timeout=10)
                result = debit_wallet(self.reseller_id, "20", "request:concurrent")
                results.put((result.status, result.transaction_id))
            finally:
                db.close_thread_connection()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())
        values = [results.get_nowait() for _ in threads]
        self.assertCountEqual([value[0] for value in values], ["posted", "duplicate"])
        self.assertEqual(values[0][1], values[1][1])
        self.assertEqual(get_balance(self.reseller_id)["balance_cents"], 8000)
        self.assertEqual(sum(r["reference"] == "request:concurrent" for r in self.ledger()), 1)
        self.assert_reconciled()

    def test_same_request_after_timeout_reuses_original_result(self):
        credit_wallet(self.reseller_id, "100", "fund:timeout")
        original = debit_wallet(self.reseller_id, "20", "request:timeout")
        # Simulate a lost response: the caller ignores it and retries later.
        retried = debit_wallet(self.reseller_id, "20", "request:timeout")
        self.assertEqual(retried.status, "duplicate")
        self.assertEqual(retried.transaction_id, original.transaction_id)
        self.assertEqual(retried.balance_cents, original.balance_cents)
        self.assertEqual(get_balance(self.reseller_id)["balance_cents"], 8000)
        self.assert_reconciled()

    def test_different_references_are_different_requests(self):
        credit_wallet(self.reseller_id, "100", "fund:different")
        first = debit_wallet(self.reseller_id, "20", "request:different-a")
        second = debit_wallet(self.reseller_id, "20", "request:different-b")
        self.assertEqual(first.status, "posted")
        self.assertEqual(second.status, "posted")
        self.assertNotEqual(first.transaction_id, second.transaction_id)
        self.assertEqual(get_balance(self.reseller_id)["balance_cents"], 6000)
        self.assert_reconciled()

    def test_failed_transaction_can_be_retried(self):
        db.cur.execute("""
            CREATE TRIGGER fail_first_idempotent_insert
            BEFORE INSERT ON reseller_wallet_transactions
            WHEN NEW.reference = 'request:failed-retry'
            BEGIN SELECT RAISE(ABORT, 'forced first attempt failure'); END
        """)
        db.conn.commit()
        try:
            with self.assertRaises(Exception):
                credit_wallet(self.reseller_id, "20", "request:failed-retry")
        finally:
            db.cur.execute("DROP TRIGGER IF EXISTS fail_first_idempotent_insert")
            db.conn.commit()
        self.assertEqual(get_balance(self.reseller_id)["balance_cents"], 0)
        self.assertIsNone(rdb.get_wallet_transaction_by_reference(
            self.reseller_id, "topup_credit", "request:failed-retry"
        ))
        retried = credit_wallet(self.reseller_id, "20", "request:failed-retry")
        self.assertEqual(retried.status, "posted")
        self.assertEqual(get_balance(self.reseller_id)["balance_cents"], 2000)
        self.assertEqual(sum(r["reference"] == "request:failed-retry" for r in self.ledger()), 1)
        self.assert_reconciled()

    def test_successful_transaction_retry_is_safe(self):
        original = credit_wallet(self.reseller_id, "20", "request:successful-retry")
        retried = credit_wallet(self.reseller_id, "20", "request:successful-retry")
        self.assertEqual(retried.status, "duplicate")
        self.assertEqual(retried.transaction_id, original.transaction_id)
        self.assertEqual(get_balance(self.reseller_id)["balance_cents"], 2000)
        self.assertEqual(sum(r["reference"] == "request:successful-retry" for r in self.ledger()), 1)
        self.assert_reconciled()

    @classmethod
    def tearDownClass(cls):
        db.close_thread_connection()
        try:
            os.unlink(_db_path)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    unittest.main()
