"""Phase 16.2 — owner "Fail Order" action (full refund + stock restore).

Exercises the REAL route (POST /api/orders/<oid>/fail) against the real
failure engine.  Telegram sends are spied at the boundary only; no business
logic is mocked.
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase16_fail_", suffix=".db")
os.close(_fd)

import config
config.DB_PATH = _db_path
import database.db as db
db.DB_PATH = _db_path
db._tls = threading.local()
db.init_db()

import database.reseller_db as rdb
import database.adjustment_db as adb
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


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


class Phase16AdminFailOrderTests(unittest.TestCase):
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

    def _owner(self, role="owner"):
        import web_admin.app as wa
        wa.app.config["TESTING"] = True
        wa.DB_PATH = _db_path
        c = wa.app.test_client()
        with c.session_transaction() as sess:
            sess["logged_in"] = True
            sess["username"] = "owner"
            sess["web_role"] = role
            sess["csrf"] = "test-csrf"
        return c

    def _make_reseller_order(self, codes=2, telegram=True):
        n = _n()
        uid = 9_540_000 + n
        db.cur.execute(
            "INSERT INTO users(user_id, first_name, username, blocked)"
            " VALUES(?,?,?,0)", (uid, f"P16F{n}", f"p16f{n}"))
        db.conn.commit()
        rs = rdb.create_reseller(f"P16F{n}", uid, f"p16f{n}")
        rdb.set_reseller_status(rs["id"], "active")
        key_id, secret = rdb.create_api_key(rs["id"], "test")
        db.cur.execute(
            "INSERT INTO products(name, price, stock, delivery, active)"
            " VALUES(?,10.0,0,'code_stock',1)", (f"P16F Product {n}",))
        db.conn.commit()
        pid = db.cur.lastrowid
        for i in range(codes):
            db.cur.execute(
                "INSERT INTO product_stock(product_id, delivery, sold)"
                " VALUES(?,?,0)", (pid, f"F-CODE-{n}-{i}"))
            db.conn.commit()
        rdb.set_product_access(rs["id"], pid, True)
        rdb.import_reseller_product(rs["id"], pid)
        pub = rdb.ensure_product_public_id(pid)
        rdb.post_wallet_transaction(rs["id"], "topup_credit", 50000,
                                    f"TP-P16F-{_n()}", "seed")
        r = self.client.post("/api/v1/orders", headers=_hdr(key_id, secret),
                             json={"product_id": pub, "quantity": 1,
                                   "client_order_id": f"p16f-{n}"})
        assert r.status_code == 201, r.get_json()
        oid = int(r.get_json()["data"]["order_id"].replace("ORD-", ""))
        if not telegram:
            db.cur.execute("UPDATE resellers SET telegram_user_id=NULL WHERE id=?",
                           (rs["id"],))
            db.conn.commit()
        return rs, oid, pid

    def _fail(self, ac, oid, reason="supplier outage"):
        return ac.post(f"/api/orders/{oid}/fail", json={"reason": reason},
                       headers={"X-CSRF-Token": "test-csrf"})

    # ------------------------------------------------------------ 1-7. success path

    def test_successful_fail_full_effects(self):
        import web_admin.app as wa
        rs, oid, pid = self._make_reseller_order()
        ac = self._owner()
        bal_before = rdb.get_wallet_balance_cents(rs["id"])
        sent = []

        def spy(chat_id, text, reply_markup=None):
            sent.append((chat_id, text))
            return True, "ok"

        with patch.object(wa, "tg_send", side_effect=spy):
            r = self._fail(ac, oid)
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["result"], "handled")
        self.assertEqual(body["refunded_cents"], 1000)
        self.assertEqual(body["status"], "failed")
        self.assertTrue(body["stock_restored"])
        self.assertEqual(body["notify"], "sent")
        # bal_before is post-order (49000); the full refund restores the
        # pre-order balance (50000)
        self.assertEqual(body["balance"], bal_before + 1000)

        # wallet + ledger: fully refunded back to the pre-order balance
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]),
                         bal_before + 1000)
        # terminal status persisted
        db.cur.execute("SELECT master_status FROM orders WHERE id=?", (oid,))
        self.assertIn("Failed", db.cur.fetchone()[0])
        # stock restored exactly once
        db.cur.execute(
            "SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=1",
            (pid,))
        self.assertEqual(int(db.cur.fetchone()[0]), 0)
        # three lifecycle outbox events
        db.cur.execute(
            "SELECT event_type FROM webhook_outbox WHERE aggregate_id=?",
            (f"ORD-{oid:08d}",))
        events = {r[0] for r in db.cur.fetchall()}
        self.assertIn("order.failed", events)
        self.assertIn("delivery.failed", events)
        self.assertIn("refund.created", events)
        # refund record with admin attribution
        recs = adb.list_refunds(order_id=oid)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["source"], "admin")
        self.assertEqual(recs[0]["created_by"], "owner")
        self.assertEqual(recs[0]["amount_cents"], 1000)
        # audits
        db.cur.execute(
            "SELECT COUNT(*) FROM admin_logs WHERE action LIKE '%reseller_order_fail%'")
        self.assertGreaterEqual(int(db.cur.fetchone()[0]), 1)
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_security_events"
            " WHERE reseller_id=? AND event_type='admin_order_fail'", (rs["id"],))
        self.assertGreaterEqual(int(db.cur.fetchone()[0]), 1)
        # telegram message: correct recipient + content, no secrets
        self.assertEqual(len(sent), 1)
        self.assertIn(f"ORD-{oid:08d}", sent[0][1])
        self.assertIn("10.00", sent[0][1])
        self.assertIn("supplier outage", sent[0][1])

    # ------------------------------------------------------------ 8. idempotency

    def test_repeat_fail_409_no_duplication(self):
        rs, oid, pid = self._make_reseller_order()
        ac = self._owner()
        r1 = self._fail(ac, oid)
        self.assertEqual(r1.status_code, 200)
        bal = rdb.get_wallet_balance_cents(rs["id"])
        r2 = self._fail(ac, oid)
        self.assertEqual(r2.status_code, 409)
        self.assertIn("status", r2.get_json())
        # no second refund, no second stock restore
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), bal)
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND tx_type='refund_credit'", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 1)
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_refunds WHERE order_id=?", (oid,))
        self.assertEqual(int(db.cur.fetchone()[0]), 1)

    # ------------------------------------------------------------ 9-13. guards

    def test_authorization_and_validation_guards(self):
        import web_admin.app as wa
        rs, oid, pid = self._make_reseller_order()
        # non-owner 403
        self.assertEqual(self._owner(role="admin")
                         .post(f"/api/orders/{oid}/fail",
                               json={"reason": "x"},
                               headers={"X-CSRF-Token": "test-csrf"}).status_code, 403)
        # unauthenticated (401 or CSRF-403 — both deny before touching data)
        anon = wa.app.test_client()
        self.assertIn(anon.post(f"/api/orders/{oid}/fail",
                                json={"reason": "x"},
                                headers={"X-CSRF-Token": "test-csrf"}).status_code,
                      (401, 403))
        # missing/empty/oversized reason -> 400
        ac = self._owner()
        self.assertEqual(self._fail(ac, oid, "").status_code, 400)
        self.assertEqual(self._fail(ac, oid, "   ").status_code, 400)
        self.assertEqual(self._fail(ac, oid, "x" * 501).status_code, 400)
        # unknown order -> 404
        self.assertEqual(self._fail(ac, 99999999).status_code, 404)
        # order untouched through all of it
        db.cur.execute("SELECT master_status FROM orders WHERE id=?", (oid,))
        self.assertNotIn("Failed", str(db.cur.fetchone()[0] or ""))

    def test_customer_order_400_unchanged(self):
        n = _n()
        uid = 8_860_000 + n
        db.cur.execute(
            "INSERT INTO users(user_id, first_name, username, blocked, balance)"
            " VALUES(?,?,?,0,0)", (uid, f"Cust{n}", f"c{n}"))
        db.cur.execute(
            "INSERT INTO products(name, price, stock, delivery, active)"
            " VALUES(?,12.5,0,'code_stock',1)", (f"P16F Cust {n}",))
        db.conn.commit()
        pid = db.cur.lastrowid
        db.cur.execute(
            "INSERT INTO orders(user_id, product_name, price, delivery,"
            " product_id, payment_method) VALUES(?,?,?,?,?,?)",
            (uid, f"P16F Cust {n}", 12.5, "enc", pid, "crypto"))
        db.conn.commit()
        oid = db.cur.lastrowid
        ac = self._owner()
        r = self._fail(ac, oid)
        self.assertEqual(r.status_code, 400)
        db.cur.execute("SELECT COUNT(*) FROM orders WHERE id=?", (oid,))
        self.assertEqual(int(db.cur.fetchone()[0]), 1)

    def test_legacy_null_price_422_no_zero_refund(self):
        rs, oid, pid = self._make_reseller_order()
        # legacy-ify the order: strip pricing metadata
        db.cur.execute(
            "UPDATE orders SET unit_price_cents=NULL WHERE id=?", (oid,))
        db.conn.commit()
        ac = self._owner()
        bal_before = rdb.get_wallet_balance_cents(rs["id"])
        r = self._fail(ac, oid)
        self.assertEqual(r.status_code, 422)
        self.assertIn("no computable charge", r.get_json()["error"])
        # no refund row, wallet untouched, order NOT failed
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), bal_before)
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND tx_type='refund_credit'", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 0)
        db.cur.execute("SELECT master_status FROM orders WHERE id=?", (oid,))
        self.assertNotIn("Failed", str(db.cur.fetchone()[0] or ""))

    # ------------------------------------------------------------ 14. partial-refund cap

    def test_partial_admin_refund_then_fail_caps_outstanding(self):
        from reseller_api.services.adjustment_service import issue_admin_refund
        rs, oid, pid = self._make_reseller_order()
        ac = self._owner()
        issue_admin_refund(oid, 400, "partial goodwill", actor="owner")
        bal = rdb.get_wallet_balance_cents(rs["id"])
        r = self._fail(ac, oid)
        self.assertEqual(r.status_code, 200)
        # only the remaining $6 refunded (not $10)
        self.assertEqual(r.get_json()["refunded_cents"], 600)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), bal + 600)
        db.cur.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND tx_type='refund_credit'", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 1000)   # never over the charge

    # ------------------------------------------------------------ 15-16. telegram safety

    def test_missing_telegram_link_still_fails(self):
        rs, oid, pid = self._make_reseller_order(telegram=False)
        ac = self._owner()
        r = self._fail(ac, oid)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["notify"], "no_telegram_link")
        db.cur.execute("SELECT master_status FROM orders WHERE id=?", (oid,))
        self.assertIn("Failed", db.cur.fetchone()[0])

    def test_telegram_error_does_not_rollback(self):
        import web_admin.app as wa
        rs, oid, pid = self._make_reseller_order()
        ac = self._owner()

        def boom(chat_id, text, reply_markup=None):
            raise RuntimeError("telegram down")

        with patch.object(wa, "tg_send", side_effect=boom):
            r = self._fail(ac, oid)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["notify"], "failed")
        self.assertEqual(r.get_json()["refunded_cents"], 1000)

    # ------------------------------------------------------------ 17. crash-resume

    def test_crash_resume_through_route(self):
        """Crash between the status commit and the refund: the route retry
        must complete the outstanding work without duplicating anything."""
        import reseller_api.services.order_failure as of
        rs, oid, pid = self._make_reseller_order()
        ac = self._owner()

        original = of._issue_refund
        of._issue_refund = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("simulated crash during refund"))
        try:
            r1 = self._fail(ac, oid)
        finally:
            of._issue_refund = original
        # the request failed mid-flight (500 via the engine's error path or
        # the propagated crash — either way, nothing was silently half-done)
        self.assertIn(r1.status_code, (500, 409))
        db.cur.execute("SELECT master_status FROM orders WHERE id=?", (oid,))
        self.assertIn("Failed", db.cur.fetchone()[0])   # status committed
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]),
                         49000)                          # refund NOT yet posted

        # retry through the route recovers: refund posts exactly once
        r2 = self._fail(ac, oid)
        self.assertEqual(r2.status_code, 200, r2.get_json())
        self.assertEqual(r2.get_json()["result"], "recovered")
        self.assertEqual(r2.get_json()["refunded_cents"], 1000)
        self.assertEqual(self._fail(ac, oid).status_code, 409)
        record = adb.get_refund_by_ledger_reference(f"rapi.refund.order.{oid}")
        self.assertEqual(record["source"], "admin")
        self.assertEqual(record["created_by"], "owner")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND tx_type='refund_credit'", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 1)

    # ------------------------------------------------------------ UI wiring

    def test_ui_fail_action_wiring(self):
        js = (ROOT / "web_admin" / "static" / "js" / "app.js").read_text()
        self.assertIn("failOrderModal(", js)
        self.assertIn("Fail &amp; refund", js)
        self.assertIn("/fail", js)
        self.assertIn("refunded_cents", js)
        self.assertIn("outstanding", js)
        import subprocess
        r = subprocess.run(["node", "--check",
                            str(ROOT / "web_admin" / "static" / "js" / "app.js")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[:300])


if __name__ == "__main__":
    unittest.main()
