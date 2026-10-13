"""Phase 14 Prompt 4 — reseller revenue, wallet & financial analytics.

Every assertion compares the analytics output against the AUTHORITATIVE
ledger (reseller_wallet_transactions) recomputed independently in the test.
Integer cents throughout; the API exposes decimal strings via from_cents.
Analytics are read-only — proven by re-running them and asserting the
ledger is unchanged.
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase14_fin_", suffix=".db")
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
from reseller_api.services.order_failure import (
    handle_post_debit_failure,
    handle_cancellation,
    handle_partial_failure,
)

_ctr = 0


def _n():
    global _ctr
    _ctr += 1
    return _ctr


def _make_reseller():
    n = _n()
    uid = 9_720_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P14F{n}", f"p14f{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P14F{n}", uid, f"p14f{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=()):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P14F Product {n}", price),
    )
    db.conn.commit()
    pid = db.cur.lastrowid
    db.cur.execute("UPDATE products SET max_per_order=25 WHERE id=?", (pid,))
    for c in codes:
        db.cur.execute(
            "INSERT INTO product_stock(product_id, delivery, sold) VALUES(?,?,0)", (pid, c)
        )
    db.conn.commit()
    return pid, rdb.ensure_product_public_id(pid)


def _grant(rid, pid):
    rdb.set_product_access(rid, pid, True)
    rdb.import_reseller_product(rid, pid)


def _topup(rid, cents):
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P14F-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


def _ledger_truth(rid):
    """Independent recomputation straight from the ledger."""
    db.cur.execute(
        "SELECT tx_type, amount_cents FROM reseller_wallet_transactions"
        " WHERE reseller_id=?", (rid,))
    rows = db.cur.fetchall()
    purchases = sum(-a for t, a in rows if t == "purchase_debit")
    adj_debits = sum(-a for t, a in rows if t == "adjustment_debit")
    credits = sum(a for t, a in rows
                  if t in ("topup_credit", "refund_credit", "adjustment_credit"))
    refunds = sum(a for t, a in rows if t == "refund_credit")
    refund_count = sum(1 for t, _a in rows if t == "refund_credit")
    return {
        "purchases": purchases,
        "purchase_count": sum(1 for t, _a in rows if t == "purchase_debit"),
        "debits": purchases + adj_debits,
        "credits": credits,
        "refunds": refunds,
        "refund_count": refund_count,
        "net_spend": purchases - refunds,
        "count": len(rows),
    }


class Phase14FinancialAnalyticsTests(unittest.TestCase):
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

        # ---------- Reseller A: rich known ledger ----------
        cls.ra, cls.ka, cls.sa = _make_reseller()
        cls.pa, cls.puba = _make_product(price=10.0,
                                         codes=[f"FA-{i}" for i in range(10)])
        _grant(cls.ra["id"], cls.pa)
        _topup(cls.ra["id"], 100000)                       # +$1000 credit
        H = _hdr(cls.ka, cls.sa)

        def order(qty, key):
            r = cls.client.post("/api/v1/orders", headers=H,
                                json={"product_id": cls.puba, "quantity": qty,
                                      "client_order_id": key})
            assert r.status_code == 201, r.get_json()
            return int(r.get_json()["data"]["order_id"].replace("ORD-", ""))

        cls.o1 = order(1, "p14f-o1")                        # -$10
        cls.o2 = order(2, "p14f-o2")                        # -$20
        o3 = order(1, "p14f-o3")
        handle_post_debit_failure(o3, reason="test")        # +$10 full refund
        o4 = order(1, "p14f-o4")
        handle_cancellation(o4, post_debit=True)            # +$10 cancel refund
        o5 = order(2, "p14f-o5")
        handle_partial_failure(o5, fulfilled_qty=1, failed_qty=1)  # +$10 partial
        # owner adjustment: +$5 credit, −$3 debit (direct ledger ops)
        rdb.post_wallet_transaction(cls.ra["id"], "adjustment_credit", 500,
                                    "ADJ-P14F-1", "goodwill")
        rdb.post_wallet_transaction(cls.ra["id"], "adjustment_debit", -300,
                                    "ADJ-P14F-2", "correction")

        # ---------- Reseller B: simple ledger ----------
        cls.rb, cls.kb, cls.sb = _make_reseller()
        cls.pb, cls.pubb = _make_product(price=25.0, codes=["FB-1"])
        _grant(cls.rb["id"], cls.pb)
        _topup(cls.rb["id"], 50000)                          # +$500
        r = cls.client.post("/api/v1/orders", headers=_hdr(cls.kb, cls.sb),
                            json={"product_id": cls.pubb, "quantity": 1,
                                  "client_order_id": "p14f-b1"})
        assert r.status_code == 201                          # −$25

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

    # ------------------------------------------------------------- reseller view

    def test_wallet_summary_matches_ledger_exactly(self):
        r = self.client.get("/api/v1/wallet/summary", headers=_hdr(self.ka, self.sa))
        self.assertEqual(r.status_code, 200, r.get_json())
        w = r.get_json()["data"]["wallet"]
        truth = _ledger_truth(self.ra["id"])

        self.assertEqual(w["total_purchases_cents"], truth["purchases"])   # 7000
        self.assertEqual(w["purchase_count"], truth["purchase_count"])     # 5
        self.assertEqual(w["total_debits_cents"], truth["debits"])         # 7300
        self.assertEqual(w["total_credits_cents"], truth["credits"])       # 103500
        self.assertEqual(w["refunds_cents"], truth["refunds"])             # 3000
        self.assertEqual(w["refund_count"], truth["refund_count"])         # 3
        self.assertEqual(w["net_spend_cents"], truth["net_spend"])         # 4000
        self.assertEqual(w["transaction_count"], truth["count"])
        self.assertEqual(w["balance_cents"],
                         rdb.get_wallet_balance_cents(self.ra["id"]))
        # decimal strings
        self.assertEqual(w["balance"], rdb.from_cents(w["balance_cents"]))
        self.assertEqual(w["net_spend"], "40.00")
        self.assertEqual(w["currency"], "USDT")
        # no float TYPES anywhere (decimal strings are the money format)
        for k, v in w.items():
            self.assertNotIsInstance(v, float, k)

    def test_reseller_b_isolation_and_balance(self):
        w = self.client.get("/api/v1/wallet/summary",
                            headers=_hdr(self.kb, self.sb)).get_json()["data"]["wallet"]
        truth = _ledger_truth(self.rb["id"])
        self.assertEqual(w["total_purchases_cents"], truth["purchases"])   # 2500
        self.assertEqual(w["refunds_cents"], 0)
        self.assertEqual(w["balance_cents"], 47500)
        # nothing of A leaks
        self.assertEqual(w["purchase_count"], 1)

    def test_analytics_are_read_only(self):
        before = _ledger_truth(self.ra["id"])["count"]
        for _ in range(3):
            self.client.get("/api/v1/wallet/summary", headers=_hdr(self.ka, self.sa))
            rdb.get_wallet_financial_summary(self.ra["id"])
        after = _ledger_truth(self.ra["id"])["count"]
        self.assertEqual(before, after)     # analytics never write

    # ------------------------------------------------------------- owner view

    def test_owner_platform_financials(self):
        import web_admin.app as wa
        wa.app.config["TESTING"] = True
        wa.DB_PATH = _db_path
        ac = wa.app.test_client()
        with ac.session_transaction() as sess:
            sess["logged_in"] = True
            sess["username"] = "owner"

        r = ac.get("/api/monitoring/financials")
        self.assertEqual(r.status_code, 200)
        f = r.get_json()["financials"]

        truth_a = _ledger_truth(self.ra["id"])
        truth_b = _ledger_truth(self.rb["id"])
        # other tests' resellers (e.g. the concurrency test) also contribute
        # to platform totals — the floor is A+B, exactness is per-reseller
        self.assertGreaterEqual(f["total_purchases_cents"],
                                truth_a["purchases"] + truth_b["purchases"])
        self.assertGreaterEqual(f["refunds_cents"],
                                truth_a["refunds"] + truth_b["refunds"])
        board = {b["reseller_id"]: b for b in f["resellers"]}
        self.assertEqual(board[self.ra["public_id"]]["total_purchases"], "70.00")
        self.assertEqual(board[self.ra["public_id"]]["refunds"], "30.00")
        self.assertEqual(board[self.ra["public_id"]]["net_spend"], "40.00")
        self.assertEqual(board[self.rb["public_id"]]["net_spend"], "25.00")
        # internal consistency: platform totals are the sum of the board
        self.assertEqual(
            f["total_purchases_cents"],
            sum(int(round(float(b["total_purchases"]) * 100)) for b in f["resellers"]))

        anon = wa.app.test_client()
        self.assertEqual(anon.get("/api/monitoring/financials").status_code, 401)

    # ------------------------------------------------------------- concurrency

    def test_concurrent_orders_financials_stay_exact(self):
        """Two racing orders on a balance for one: the analytics must show
        exactly one purchase — matching the ledger, never negative."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["CC-1", "CC-2"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 1000)

        barrier = threading.Barrier(2)
        out = []

        def worker(i):
            c = self.app.test_client()
            barrier.wait()
            out.append(c.post("/api/v1/orders", headers=_hdr(key, sec),
                              json={"product_id": pub, "quantity": 1,
                                    "client_order_id": f"p14f-race-{i}"}).status_code)

        a, b = threading.Thread(target=worker, args=(1,)), threading.Thread(target=worker, args=(2,))
        a.start(); b.start(); a.join(); b.join()
        self.assertEqual(sorted(out), [201, 402])

        w = rdb.get_wallet_financial_summary(rs["id"])
        truth = _ledger_truth(rs["id"])
        self.assertEqual(truth["purchases"], 1000)
        self.assertEqual(w["total_purchases_cents"], truth["purchases"])
        self.assertEqual(w["balance_cents"], 0)            # never negative
        self.assertEqual(w["net_spend_cents"], 1000)

    def test_privacy_no_internals(self):
        import json as _json
        w = self.client.get("/api/v1/wallet/summary",
                            headers=_hdr(self.ka, self.sa)).get_json()
        flat = _json.dumps(w).lower()
        for banned in ("secret", "reference", "rapi.", "description",
                       "user_id", "telegram", "reseller_id"):
            self.assertNotIn(banned, flat)


if __name__ == "__main__":
    unittest.main()
