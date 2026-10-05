"""Adversarial Phase 6 wallet security review."""
import os
import sys
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
fd, path = tempfile.mkstemp(prefix="oggy_wallet_security_", suffix=".db")
os.close(fd)
import config
config.DB_PATH = path
import database.db as db
db.DB_PATH = path
db._tls = threading.local()
db.init_db()
import database.reseller_db as rdb
from reseller_api.context import ApiRequestContext
from reseller_api.services.authentication import authenticate_credentials
from reseller_api.services.wallet import (
    WalletError, credit_wallet, debit_wallet, refund_wallet,
    get_authenticated_balance, get_balance,
)
from web_admin.wallet_admin_service import AdminWalletError, credit_as_admin, wallet_history


class WalletSecurityReviewTests(unittest.TestCase):
    counter = 0

    def setUp(self):
        type(self).counter += 1
        n = self.counter
        self.uid = 998000 + n
        db.cur.execute("INSERT INTO users(user_id,first_name,username,blocked) VALUES(?,?,?,0)", (self.uid, "Security", f"wallet_sec_{n}"))
        db.conn.commit()
        self.reseller = rdb.create_reseller(f"Security {n}", self.uid, f"wallet_sec_{n}")
        self.rid = self.reseller["id"]
        rdb.set_reseller_status(self.rid, "active")
        self.key, self.secret = rdb.create_api_key(self.rid, "test")

    def context(self):
        p = authenticate_credentials(self.key, self.secret, "test")
        return ApiRequestContext("b" * 32, p.environment, p.reseller_id, p.reseller_public_id, p.api_key_id, p.api_key_public_id)

    def test_negative_balance_and_amount_sign_manipulation(self):
        credit_wallet(self.rid, "10", "security:fund")
        with self.assertRaises(WalletError) as caught:
            debit_wallet(self.rid, "11", "security:overdraw")
        self.assertEqual(caught.exception.code, "insufficient_funds")
        for tx_type, amount in (("topup_credit", -1), ("purchase_debit", 1)):
            with self.assertRaises(ValueError):
                rdb.post_wallet_transaction(self.rid, tx_type, amount, "security:sign")
        self.assertEqual(get_balance(self.rid)["balance_cents"], 1000)

    def test_low_level_money_type_and_balance_overflow_protection(self):
        for value in (1.2, "100", True, None):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                rdb.post_wallet_transaction(self.rid, "topup_credit", value, "security:type")
        maximum = rdb.MAX_WALLET_BALANCE_CENTS
        status, _ = rdb.post_wallet_transaction(self.rid, "topup_credit", maximum, "security:max")
        self.assertEqual(status, "posted")
        with self.assertRaises(WalletError) as caught:
            credit_wallet(self.rid, "0.01", "security:overflow")
        self.assertEqual(caught.exception.code, "balance_limit_exceeded")
        self.assertEqual(get_balance(self.rid)["balance_cents"], maximum)
        self.assertIsNone(rdb.get_wallet_transaction_by_reference(self.rid, "topup_credit", "security:overflow"))

    def test_decimal_rounding_and_float_rejection(self):
        self.assertEqual(rdb.to_cents(Decimal("1.005")), 101)
        self.assertEqual(rdb.to_cents("1.004"), 100)
        with self.assertRaises(TypeError):
            rdb.to_cents(0.1)
        with self.assertRaises(WalletError):
            credit_wallet(self.rid, 0.1, "security:float")

    def test_duplicate_debit_credit_and_refund_change_balance_once(self):
        first_credit = credit_wallet(self.rid, "100", "security:credit")
        duplicate_credit = credit_wallet(self.rid, "100", "security:credit")
        first_debit = debit_wallet(self.rid, "20", "security:order")
        duplicate_debit = debit_wallet(self.rid, "20", "security:order")
        first_refund = refund_wallet(self.rid, "20", "security:order")
        duplicate_refund = refund_wallet(self.rid, "20", "security:order")
        self.assertEqual([duplicate_credit.status, duplicate_debit.status, duplicate_refund.status], ["duplicate"] * 3)
        self.assertEqual(duplicate_credit.transaction_id, first_credit.transaction_id)
        self.assertEqual(duplicate_debit.transaction_id, first_debit.transaction_id)
        self.assertEqual(duplicate_refund.transaction_id, first_refund.transaction_id)
        self.assertEqual(get_balance(self.rid)["balance_cents"], 10000)
        self.assertEqual(len(rdb.list_wallet_transactions(self.rid)), 3)

    def test_reference_currency_order_reference_and_payload_manipulation(self):
        invalid_refs = ("", "../wallet", "x' OR 1=1 --", "x" * 129, "line\nbreak")
        for reference in invalid_refs:
            with self.subTest(reference=reference), self.assertRaises((WalletError, ValueError)):
                credit_wallet(self.rid, "1", reference)
        with self.assertRaises(ValueError):
            rdb.post_wallet_transaction(self.rid, "topup_credit", 100, "security:currency", currency="USD")
        with self.assertRaises(WalletError):
            debit_wallet(self.rid, "1", "security:order-ref", related_order_reference="../other")
        with self.assertRaises(WalletError):
            credit_wallet(self.rid, "1", "security:description", "x" * 501)

    def test_idor_reseller_manipulation_and_authentication_bypass(self):
        credit_wallet(self.rid, "10", "security:own")
        uid2 = self.uid + 10000
        db.cur.execute("INSERT INTO users(user_id,first_name,username,blocked) VALUES(?,?,?,0)", (uid2, "Other", f"other_sec_{uid2}")); db.conn.commit()
        other = rdb.create_reseller("Other", uid2, f"other_sec_{uid2}"); rdb.set_reseller_status(other["id"], "active")
        credit_wallet(other["id"], "99", "security:other")
        context = self.context()
        self.assertEqual(get_authenticated_balance(context, "test")["balance"], "10.00")
        forged = ApiRequestContext(context.request_id, context.environment, other["id"], other["public_id"], context.api_key_id, context.api_key_public_id)
        with self.assertRaises(WalletError):
            get_authenticated_balance(forged, "test")
        self.assertIsNone(authenticate_credentials(self.key, "wrong-secret", "test"))

    def test_admin_authorization_cannot_be_bypassed(self):
        for role in (None, "", "admin", "reseller"):
            with self.subTest(role=role), self.assertRaises(AdminWalletError) as caught:
                credit_as_admin(role, self.rid, "10", "security:admin", "Attempt")
            self.assertEqual(caught.exception.http_status, 403)
        self.assertEqual(get_balance(self.rid)["balance_cents"], 0)

    def test_rollback_prevents_balance_without_ledger(self):
        base = rdb.get_cursor()
        class FailingCursor:
            def execute(self, sql, params=()):
                if sql.strip().upper().startswith("INSERT INTO RESELLER_WALLET_TRANSACTIONS"):
                    raise RuntimeError("forced insert failure")
                return base.execute(sql, params)
            def __getattr__(self, name):
                return getattr(base, name)
        with patch.object(rdb, "get_cursor", return_value=FailingCursor()):
            with self.assertRaises(RuntimeError):
                credit_wallet(self.rid, "10", "security:rollback")
        self.assertEqual(get_balance(self.rid)["balance_cents"], 0)
        self.assertEqual(rdb.list_wallet_transactions(self.rid), [])

    def test_ledger_update_and_delete_tampering_are_blocked(self):
        result = credit_wallet(self.rid, "10", "security:immutable")
        for sql in (
            "UPDATE reseller_wallet_transactions SET amount_cents=999 WHERE id=?",
            "DELETE FROM reseller_wallet_transactions WHERE id=?",
        ):
            with self.assertRaises(Exception):
                db.cur.execute(sql, (result.transaction_id,))
            db.conn.rollback()
        row = rdb.get_wallet_transaction_by_reference(self.rid, "topup_credit", "security:immutable")
        self.assertEqual(row["amount_cents"], 1000)

    def test_wallet_search_is_parameterized_and_balance_api_is_minimal(self):
        credit_as_admin("owner", self.rid, "10", "security:search", "Safe reason")
        injected = wallet_history(self.rid, search="' OR 1=1 --")
        self.assertEqual(injected["total"], 0)
        self.assertEqual(wallet_history(self.rid)["total"], 1)
        source = (ROOT / "reseller_api/controllers/v1/balance.py").read_text()
        self.assertIn('wallet["balance"]', source)
        self.assertIn('wallet["currency"]', source)
        for leaked in ('"balance_cents"', '"reseller_id"', '"transactions"', '"api_key"'):
            self.assertNotIn(leaked, source[source.index("return success_response"):])

    @classmethod
    def tearDownClass(cls):
        db.close_thread_connection()
        try: os.unlink(path)
        except FileNotFoundError: pass


if __name__ == "__main__":
    unittest.main()
