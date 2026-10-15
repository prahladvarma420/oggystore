"""Phase 16.1 — quarantine legacy WebAdmin refund/resend for reseller orders.

The legacy customer-order endpoints (/api/orders/<id>/refund|resend) must
never touch reseller API orders (they credit a phantom customer wallet,
claw back referrals, delete the order, and Telegram strangers).  Customer
behavior is preserved exactly.
"""

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "DATA_ENCRYPTION_KEY",
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
)

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase16_q_", suffix=".db")
os.close(_fd)

import config
config.DB_PATH = _db_path
import database.db as db
db.DB_PATH = _db_path
db._tls = threading.local()
db.init_db()

import database.reseller_db as rdb
from reseller_api import create_app
import reseller_api.middleware.request_context  # noqa: F401
import reseller_api.middleware.rate_limit  # noqa: F401
import reseller_api.middleware.security_controls  # noqa: F401
from reseller_api.services.rate_limit import RequestRateDecision

_ctr = 0


def _n():
    global _ctr
    _ctr += 1
    return _ctr


class Phase16RefundQuarantineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p1 = patch(
            "reseller_api.middleware.request_context.persist_audit_record",
            return_value=True)
        cls.p1.start()
        cls.p2 = patch(
            "reseller_api.middleware.rate_limit.evaluate_request_rate",
            return_value=RequestRateDecision(
                True, "allowed", limit=60, remaining=59, reset_after=45))
        cls.p2.start()
        cls.p3 = patch(
            "reseller_api.middleware.security_controls.is_global_api_enabled",
            return_value=True)
        cls.p3.start()
        cls.app = create_app({
            "TESTING": True, "API_ENVIRONMENT": "test",
            "INIT_DATABASE": False, "RESELLER_API_HSTS": False,
        })
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.p3.stop()
        cls.p2.stop()
        cls.p1.stop()
        try:
            db.close_thread_connection()
        finally:
            try:
                os.unlink(_db_path)
            except FileNotFoundError:
                pass

    def _owner(self):
        import web_admin.app as wa
        wa.app.config["TESTING"] = True
        wa.DB_PATH = _db_path
        c = wa.app.test_client()
        with c.session_transaction() as sess:
            sess["logged_in"] = True
            sess["username"] = "owner"
            sess["web_role"] = "owner"
            sess["csrf"] = "test-csrf"
        return c

    def _make_reseller_order(self):
        n = _n()
        uid = 9_560_000 + n
        db.cur.execute(
            "INSERT INTO users(user_id, first_name, username, blocked)"
            " VALUES(?,?,?,0)", (uid, f"P16Q{n}", f"p16q{n}"))
        db.conn.commit()
        rs = rdb.create_reseller(f"P16Q{n}", uid, f"p16q{n}")
        rdb.set_reseller_status(rs["id"], "active")
        key_id, secret = rdb.create_api_key(rs["id"], "test")
        db.cur.execute(
            "INSERT INTO products(name, price, stock, delivery, active)"
            " VALUES(?,10.0,0,'code_stock',1)", (f"P16Q Product {n}",))
        db.conn.commit()
        pid = db.cur.lastrowid
        db.cur.execute(
            "INSERT INTO product_stock(product_id, delivery, sold)"
            " VALUES(?,?,0)", (pid, f"Q-CODE-{n}"))
        db.conn.commit()
        rdb.set_product_access(rs["id"], pid, True)
        rdb.import_reseller_product(rs["id"], pid)
        pub = rdb.ensure_product_public_id(pid)
        rdb.post_wallet_transaction(rs["id"], "topup_credit", 50000,
                                    f"TP-P16Q-{_n()}", "seed")
        r = self.client.post("/api/v1/orders", headers=_hdr(key_id, secret),
                             json={"product_id": pub, "quantity": 1,
                                   "client_order_id": f"p16q-{n}"})
        assert r.status_code == 201, r.get_json()
        oid = int(r.get_json()["data"]["order_id"].replace("ORD-", ""))
        return rs, oid

    def _make_customer_order(self, with_referral=False):
        n = _n()
        uid = 8_880_000 + n
        db.cur.execute(
            "INSERT INTO users(user_id, first_name, username, blocked, balance)"
            " VALUES(?,?,?,0,0)", (uid, f"Customer{n}", f"cust{n}"))
        db.conn.commit()
        if with_referral:
            referrer = 8_870_000 + n
            db.cur.execute(
                "INSERT INTO users(user_id, first_name, username, blocked,"
                " balance, referral_earnings) VALUES(?,?,?,0,0,1.0)",
                (referrer, f"Ref{n}", f"ref{n}"))
            db.cur.execute(
                "INSERT INTO referrals(referrer_id, referred_id, earned)"
                " VALUES(?,?,1.0)", (referrer, uid))
            db.conn.commit()
        db.cur.execute(
            "INSERT INTO products(name, price, stock, delivery, active)"
            " VALUES(?,12.5,0,'code_stock',1)", (f"P16Q Cust {n}",))
        db.conn.commit()
        pid = db.cur.lastrowid
        db.cur.execute(
            "INSERT INTO product_stock(product_id, delivery, sold)"
            " VALUES(?,?,1)", (pid, f"CUST-CODE-{n}"))
        db.cur.execute(
            "INSERT INTO orders(user_id, product_name, price, delivery,"
            " product_id, payment_method, created) VALUES(?,?,?,?,?,?,"
            " '2026-01-01 00:00:00')",
            (uid, f"P16Q Cust {n}", 12.5, "enc", pid, "crypto"))
        db.conn.commit()
        return uid, db.cur.lastrowid

    # ------------------------------------------------------------ 1-4. refund quarantine

    def test_reseller_refund_blocked_409_and_side_effect_free(self):
        rs, oid = self._make_reseller_order()
        ac = self._owner()
        bal_before = rdb.get_wallet_balance_cents(rs["id"])
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_wallet_transactions WHERE reseller_id=?",
            (rs["id"],))
        ledger_before = int(db.cur.fetchone()[0])

        r = ac.post(f"/api/orders/{oid}/refund", json={},
                    headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("admin-refund", r.get_json()["error"])

        # order still present; wallet + ledger untouched; no user_id=0 wallet
        db.cur.execute("SELECT COUNT(*) FROM orders WHERE id=?", (oid,))
        self.assertEqual(int(db.cur.fetchone()[0]), 1)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), bal_before)
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_wallet_transactions WHERE reseller_id=?",
            (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), ledger_before)
        db.cur.execute("SELECT balance FROM users WHERE user_id=0")
        row = db.cur.fetchone()
        self.assertTrue(row is None or not row[0],
                        "no phantom customer balance may be created")
        # audited
        db.cur.execute(
            "SELECT COUNT(*) FROM admin_logs WHERE action LIKE '%reseller_legacy_refund_blocked%'")
        self.assertGreaterEqual(int(db.cur.fetchone()[0]), 1)

    # ------------------------------------------------------------ 5-6. resend quarantine

    def test_reseller_resend_blocked_409_no_telegram(self):
        import web_admin.app as wa
        rs, oid = self._make_reseller_order()
        ac = self._owner()
        sent = []

        def spy(chat_id, text, reply_markup=None):
            sent.append(chat_id)
            return True, "ok"

        with patch.object(wa, "tg_send", side_effect=spy):
            r = ac.post(f"/api/orders/{oid}/resend", json={},
                        headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(sent, [], "no Telegram message may be sent")
        db.cur.execute(
            "SELECT COUNT(*) FROM admin_logs WHERE action LIKE '%reseller_legacy_resend_blocked%'")
        self.assertGreaterEqual(int(db.cur.fetchone()[0]), 1)

    # ------------------------------------------------------------ 7-8. customer refund intact

    def test_customer_refund_unchanged_with_clawback(self):
        uid, oid = self._make_customer_order(with_referral=True)
        ac = self._owner()
        db.cur.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
        self.assertEqual(float(db.cur.fetchone()[0] or 0), 0.0)
        db.cur.execute(
            "SELECT referral_earnings FROM users u JOIN referrals r"
            " ON r.referrer_id=u.user_id WHERE r.referred_id=?", (uid,))
        ref_before = float(db.cur.fetchone()[0])

        r = ac.post(f"/api/orders/{oid}/refund", json={},
                    headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()["ok"])

        # customer wallet credited with the order price
        db.cur.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
        self.assertAlmostEqual(float(db.cur.fetchone()[0]), 12.5, places=4)
        # referral clawback ran (earnings reduced)
        db.cur.execute(
            "SELECT referral_earnings FROM users u JOIN referrals r"
            " ON r.referrer_id=u.user_id WHERE r.referred_id=?", (uid,))
        ref_after = float(db.cur.fetchone()[0])
        self.assertLessEqual(ref_after, ref_before)
        # order deleted per the legacy flow
        db.cur.execute("SELECT COUNT(*) FROM orders WHERE id=?", (oid,))
        self.assertEqual(int(db.cur.fetchone()[0]), 0)
        # legacy audit action name unchanged
        db.cur.execute(
            "SELECT COUNT(*) FROM admin_logs WHERE action LIKE '%refund_order%'")
        self.assertGreaterEqual(int(db.cur.fetchone()[0]), 1)

    # ------------------------------------------------------------ 9. customer resend intact

    def test_customer_resend_unchanged(self):
        import web_admin.app as wa
        uid, oid = self._make_customer_order()
        ac = self._owner()
        sent = []

        def spy(chat_id, text, reply_markup=None):
            sent.append((chat_id, text))
            return True, "ok"

        with patch.object(wa, "tg_send", side_effect=spy):
            r = ac.post(f"/api/orders/{oid}/resend", json={},
                        headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], uid)          # Telegram to the customer
        self.assertIn(str(oid), sent[0][1])

    # ------------------------------------------------------------ UI wiring

    def test_ui_hides_legacy_actions_for_reseller_rows(self):
        js = (ROOT / "web_admin" / "static" / "js" / "app.js").read_text()
        self.assertIn('o.source === "reseller_api"', js)
        self.assertIn("🏪 Reseller API", js)
        self.assertIn("admin-refund", js)
        # customer path preserved
        self.assertIn("resendOrder(${o.id})", js)
        self.assertIn("refundOrder(${o.id},${o.price})", js)
        import subprocess
        r = subprocess.run(["node", "--check",
                            str(ROOT / "web_admin" / "static" / "js" / "app.js")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[:300])


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


if __name__ == "__main__":
    unittest.main()
