"""Framework-neutral tests for reseller wallet Web Admin controls."""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_fd, _db_path = tempfile.mkstemp(prefix="oggy_wallet_admin_", suffix=".db")
os.close(_fd)
import config
config.DB_PATH = _db_path
import database.db as db
db.DB_PATH = _db_path
db._tls = threading.local()
db.init_db()
import database.reseller_db as rdb
from reseller_api.services.wallet import debit_wallet, refund_wallet
from web_admin.wallet_admin_service import (
    AdminWalletError,
    credit_as_admin,
    wallet_history,
)


class ResellerWalletWebAdminTests(unittest.TestCase):
    counter = 0

    def setUp(self):
        type(self).counter += 1
        n = self.counter
        uid = 995000 + n
        db.cur.execute(
            "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
            (uid, "Wallet Admin", f"wallet_admin_{n}"),
        )
        db.conn.commit()
        reseller = rdb.create_reseller(
            f"Wallet Admin {n}", uid, f"wallet_admin_{n}"
        )
        self.reseller_id = reseller["id"]
        rdb.set_reseller_status(self.reseller_id, "active")

    def test_authorized_owner_credit_is_atomic_ledger_entry(self):
        result = credit_as_admin(
            "owner", self.reseller_id, "25.50", "manual:credit-1",
            "Approved account credit",
        )
        self.assertEqual(result.status, "posted")
        self.assertEqual(result.balance_cents, 2550)
        row = rdb.get_wallet_transaction_by_reference(
            self.reseller_id, "adjustment_credit", "manual:credit-1"
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["amount_cents"], 2550)
        self.assertEqual(row["balance_after_cents"], 2550)
        self.assertEqual(row["description"], "Approved account credit")

    def test_unauthorized_admin_cannot_credit(self):
        for role in ("admin", "", None):
            with self.subTest(role=role), self.assertRaises(AdminWalletError) as caught:
                credit_as_admin(role, self.reseller_id, "10", "unauthorized:1", "No")
            self.assertEqual(caught.exception.code, "authorization_denied")
            self.assertEqual(caught.exception.http_status, 403)
        self.assertEqual(rdb.get_wallet_balance_cents(self.reseller_id), 0)
        self.assertEqual(rdb.list_wallet_transactions(self.reseller_id), [])

    def test_invalid_zero_and_negative_amounts_are_rejected(self):
        for amount in ("bad", "0", "-1", 1.5, None):
            with self.subTest(amount=amount), self.assertRaises(AdminWalletError):
                credit_as_admin(
                    "owner", self.reseller_id, amount,
                    "manual:invalid", "Invalid amount test",
                )
        self.assertEqual(rdb.get_wallet_balance_cents(self.reseller_id), 0)

    def test_reference_and_reason_are_required(self):
        with self.assertRaises(AdminWalletError) as missing_reference:
            credit_as_admin("owner", self.reseller_id, "10", "", "Reason")
        self.assertEqual(missing_reference.exception.code, "invalid_reference")
        with self.assertRaises(AdminWalletError) as missing_reason:
            credit_as_admin("owner", self.reseller_id, "10", "manual:reason", "")
        self.assertEqual(missing_reason.exception.code, "invalid_reason")

    def test_duplicate_reference_reuses_original_transaction(self):
        first = credit_as_admin(
            "owner", self.reseller_id, "20", "manual:duplicate", "One credit"
        )
        second = credit_as_admin(
            "owner", self.reseller_id, "20", "manual:duplicate", "One credit"
        )
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(second.transaction_id, first.transaction_id)
        self.assertEqual(rdb.get_wallet_balance_cents(self.reseller_id), 2000)
        self.assertEqual(len(rdb.list_wallet_transactions(self.reseller_id)), 1)

    def test_transaction_history_search_and_type_filter(self):
        credit_as_admin("owner", self.reseller_id, "100", "manual:history", "Opening credit")
        debit_wallet(
            self.reseller_id, "15", "order:history", "Order charge",
            related_order_reference="order:history",
        )
        refund_wallet(
            self.reseller_id, "15", "order:history", "Order refund",
            related_order_reference="order:history",
        )
        all_rows = wallet_history(self.reseller_id)
        self.assertEqual(all_rows["total"], 3)
        self.assertEqual(all_rows["balance"]["balance_cents"], 10000)
        debits = wallet_history(self.reseller_id, tx_type="purchase_debit")
        self.assertEqual(debits["total"], 1)
        self.assertEqual(debits["transactions"][0]["reference"], "order:history")
        refunds = wallet_history(self.reseller_id, tx_type="refund_credit")
        self.assertEqual(refunds["total"], 1)
        searched = wallet_history(self.reseller_id, search="Opening credit")
        self.assertEqual(searched["total"], 1)
        related = wallet_history(self.reseller_id, search="order:history")
        self.assertEqual(related["total"], 2)
        literal_wildcard = wallet_history(self.reseller_id, search="%")
        self.assertEqual(literal_wildcard["total"], 0)
        self.assertTrue(all("related_order_reference" in row for row in all_rows["transactions"]))

    def test_routes_require_login_owner_and_audit(self):
        source = (ROOT / "web_admin" / "app.py").read_text(encoding="utf-8")
        credit_start = source.index("def api_reseller_wallet_credit")
        decorators = source[max(0, credit_start - 250):credit_start]
        body = source[credit_start:source.index("\n\n@app.route", credit_start)]
        self.assertIn("@login_required", decorators)
        self.assertIn("@owner_required", decorators)
        self.assertIn("log_action(", body)
        self.assertIn("log_security_event(", body)
        self.assertNotIn("secret", body.lower())

    @classmethod
    def tearDownClass(cls):
        db.close_thread_connection()
        try:
            os.unlink(_db_path)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    unittest.main()
