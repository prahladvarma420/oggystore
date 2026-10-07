"""Real multi-connection concurrency tests for the reseller wallet ledger."""
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_wallet_concurrency_", suffix=".db")
os.close(_fd)
import config
config.DB_PATH = _db_path
import database.db as db
db.DB_PATH = _db_path
db._tls = threading.local()
db.init_db()
import database.reseller_db as rdb
from reseller_api.services.wallet import WalletError, credit_wallet, debit_wallet, get_balance


class WalletConcurrencyTests(unittest.TestCase):
    counter = 0

    def setUp(self):
        type(self).counter += 1
        n = self.counter
        uid = 980000 + n
        db.cur.execute(
            "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
            (uid, "Concurrency", f"wallet_concurrency_{n}"),
        )
        db.conn.commit()
        self.reseller = rdb.create_reseller(
            f"Concurrency {n}", uid, f"wallet_concurrency_{n}"
        )
        self.reseller_id = self.reseller["id"]
        rdb.set_reseller_status(self.reseller_id, "active")

    def run_concurrently(self, operations):
        barrier = threading.Barrier(len(operations))
        results = queue.Queue()

        def worker(operation):
            try:
                barrier.wait(timeout=10)
                result = operation()
                results.put(("ok", result.status))
            except WalletError as exc:
                results.put(("wallet_error", exc.code))
            except Exception as exc:
                results.put(("exception", str(exc)))
            finally:
                db.close_thread_connection()

        threads = [threading.Thread(target=worker, args=(op,)) for op in operations]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive(), "concurrent wallet operation hung")
        return [results.get_nowait() for _ in operations]

    def assert_ledger_reconciles(self):
        rows = sorted(rdb.list_wallet_transactions(self.reseller_id, 1000), key=lambda x: x["id"])
        running = 0
        for row in rows:
            running += int(row["amount_cents"])
            self.assertGreaterEqual(running, 0)
            self.assertEqual(int(row["balance_after_cents"]), running)
        balance = get_balance(self.reseller_id)["balance_cents"]
        self.assertGreaterEqual(balance, 0)
        self.assertEqual(balance, running)
        return rows

    def test_two_simultaneous_credits_have_no_lost_update(self):
        results = self.run_concurrently([
            lambda: credit_wallet(self.reseller_id, "50", "credit:a"),
            lambda: credit_wallet(self.reseller_id, "50", "credit:b"),
        ])
        self.assertEqual(results.count(("ok", "posted")), 2)
        self.assertEqual(get_balance(self.reseller_id)["balance_cents"], 10000)
        self.assert_ledger_reconciles()

    def test_two_simultaneous_debits_both_succeed_when_funded(self):
        credit_wallet(self.reseller_id, "200", "fund:debits")
        results = self.run_concurrently([
            lambda: debit_wallet(self.reseller_id, "80", "debit:a"),
            lambda: debit_wallet(self.reseller_id, "80", "debit:b"),
        ])
        self.assertEqual(results.count(("ok", "posted")), 2)
        self.assertEqual(get_balance(self.reseller_id)["balance_cents"], 4000)
        self.assert_ledger_reconciles()

    def test_credit_and_debit_simultaneously(self):
        credit_wallet(self.reseller_id, "100", "fund:mixed")
        results = self.run_concurrently([
            lambda: credit_wallet(self.reseller_id, "50", "mixed:credit"),
            lambda: debit_wallet(self.reseller_id, "80", "mixed:debit"),
        ])
        self.assertEqual(results.count(("ok", "posted")), 2)
        self.assertEqual(get_balance(self.reseller_id)["balance_cents"], 7000)
        self.assert_ledger_reconciles()

    def test_two_debits_exceeding_balance_only_one_succeeds(self):
        credit_wallet(self.reseller_id, "100", "fund:overdraw")
        results = self.run_concurrently([
            lambda: debit_wallet(self.reseller_id, "80", "overdraw:a"),
            lambda: debit_wallet(self.reseller_id, "80", "overdraw:b"),
        ])
        self.assertEqual(results.count(("ok", "posted")), 1)
        self.assertEqual(results.count(("wallet_error", "insufficient_funds")), 1)
        self.assertEqual(get_balance(self.reseller_id)["balance_cents"], 2000)
        rows = self.assert_ledger_reconciles()
        self.assertEqual(sum(r["tx_type"] == "purchase_debit" for r in rows), 1)

    def test_multiple_simultaneous_small_debits(self):
        credit_wallet(self.reseller_id, "100", "fund:small")
        operations = [
            (lambda i=i: debit_wallet(self.reseller_id, "5", f"small:{i}"))
            for i in range(20)
        ]
        results = self.run_concurrently(operations)
        self.assertEqual(results.count(("ok", "posted")), 20)
        self.assertEqual(get_balance(self.reseller_id)["balance_cents"], 0)
        self.assert_ledger_reconciles()

    def test_same_transaction_reference_is_debited_once(self):
        credit_wallet(self.reseller_id, "100", "fund:idempotency")
        results = self.run_concurrently([
            lambda: debit_wallet(self.reseller_id, "30", "same:debit"),
            lambda: debit_wallet(self.reseller_id, "30", "same:debit"),
        ])
        self.assertEqual(results.count(("ok", "posted")), 1)
        self.assertEqual(results.count(("ok", "duplicate")), 1)
        self.assertEqual(get_balance(self.reseller_id)["balance_cents"], 7000)
        rows = self.assert_ledger_reconciles()
        self.assertEqual(sum(r["reference"] == "same:debit" for r in rows), 1)

    def test_failed_ledger_insert_rolls_back_during_concurrency(self):
        credit_wallet(self.reseller_id, "100", "fund:rollback")
        db.cur.execute("""
            CREATE TRIGGER fail_one_wallet_insert
            BEFORE INSERT ON reseller_wallet_transactions
            WHEN NEW.reference = 'fail:concurrent'
            BEGIN SELECT RAISE(ABORT, 'forced concurrent ledger failure'); END
        """)
        db.conn.commit()
        try:
            results = self.run_concurrently([
                lambda: credit_wallet(self.reseller_id, "30", "fail:concurrent"),
                lambda: credit_wallet(self.reseller_id, "40", "pass:concurrent"),
            ])
        finally:
            db.cur.execute("DROP TRIGGER IF EXISTS fail_one_wallet_insert")
            db.conn.commit()
        self.assertEqual(results.count(("ok", "posted")), 1)
        self.assertEqual(sum(kind == "exception" for kind, _ in results), 1)
        self.assertEqual(get_balance(self.reseller_id)["balance_cents"], 14000)
        self.assertIsNone(rdb.get_wallet_transaction_by_reference(
            self.reseller_id, "topup_credit", "fail:concurrent"
        ))
        self.assert_ledger_reconciles()

    @classmethod
    def tearDownClass(cls):
        db.close_thread_connection()
        try:
            os.unlink(_db_path)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    unittest.main()
