"""Phase 14 Prompt 3 — reseller order & sales analytics.

Analytics are verified against a KNOWN set of orders built through the real
API, with real failure/cancellation/partial-refund events from the Phase 8
failure handlers.  Sources: master orders table + refund ledger (never
client fields).  Integer cents end to end; display strings via from_cents.
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase14_sales_", suffix=".db")
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
    uid = 9_730_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P14S{n}", f"p14s{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P14S{n}", uid, f"p14s{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=()):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P14S Product {n}", price),
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P14S-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


def _oid(resp):
    return int(resp.get_json()["data"]["order_id"].replace("ORD-", ""))


class Phase14SalesAnalyticsTests(unittest.TestCase):
    """One shared, fully-known workload per reseller; metrics asserted exactly."""

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

        # ---- Reseller A: completed x2 (A1 $10x1, A2 $10x2), failed ($10),
        #      cancelled ($10), partial ($20 x2 units, 1 refunded)
        cls.ra, cls.ka, cls.sa = _make_reseller()
        cls.pa, cls.puba = _make_product(price=10.0,
                                         codes=[f"A-{i}" for i in range(10)])
        _grant(cls.ra["id"], cls.pa)
        _topup(cls.ra["id"], 200000)
        H = _hdr(cls.ka, cls.sa)

        def order(qty, key):
            r = cls.client.post("/api/v1/orders", headers=H,
                                json={"product_id": cls.puba, "quantity": qty,
                                      "client_order_id": key})
            assert r.status_code == 201, r.get_json()
            return _oid(r)

        cls.a_ok1 = order(1, "p14-a-ok1")          # $10 completed
        cls.a_ok2 = order(2, "p14-a-ok2")          # $20 completed
        cls.a_fail = order(1, "p14-a-fail")
        handle_post_debit_failure(cls.a_fail, reason="test")     # +$10 refund
        cls.a_cancel = order(1, "p14-a-cancel")
        handle_cancellation(cls.a_cancel, post_debit=True)       # +$10 refund
        cls.a_part = order(2, "p14-a-part")
        handle_partial_failure(cls.a_part, fulfilled_qty=1, failed_qty=1)  # +$10

        # ---- Reseller B: one completed $30 order (price 15 x2)
        cls.rb, cls.kb, cls.sb = _make_reseller()
        cls.pb, cls.pubb = _make_product(price=15.0, codes=["B-1", "B-2"])
        _grant(cls.rb["id"], cls.pb)
        _topup(cls.rb["id"], 100000)
        r = cls.client.post("/api/v1/orders", headers=_hdr(cls.kb, cls.sb),
                            json={"product_id": cls.pubb, "quantity": 2,
                                  "client_order_id": "p14-b-ok"})
        assert r.status_code == 201
        cls.b_ok = _oid(r)

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

    # ------------------------------------------------------------- exactness

    def test_reseller_a_metrics_exact(self):
        """A: gross = 10+20+10+10+20 = $70 (5 orders); refunds = 10+10+10=$30;
        net $40; completed sales = $30; AOV = 70/5 = $14."""
        r = self.client.get("/api/v1/analytics/sales", headers=_hdr(self.ka, self.sa))
        self.assertEqual(r.status_code, 200, r.get_json())
        s = r.get_json()["data"]["sales"]

        self.assertEqual(s["orders_total"], 5)
        self.assertEqual(s["completed"], 3)        # ok1 + ok2 + partial(1 delivered unit)
        self.assertEqual(s["failed"], 1)
        self.assertEqual(s["cancelled"], 1)
        self.assertEqual(s["pending"], 0)
        self.assertEqual(s["refunded_orders"], 3)  # failed + cancelled + partial
        self.assertEqual(s["quantity_sold"], 4)    # 1+2 completed + 1 partial-kept

        self.assertEqual(s["gross_sales_cents"], 7000)
        self.assertEqual(s["gross_sales"], "70.00")
        self.assertEqual(s["refunds_full_cents"], 1000)
        self.assertEqual(s["refunds_cancellation_cents"], 1000)
        self.assertEqual(s["refunds_partial_cents"], 1000)
        self.assertEqual(s["refunds_total_cents"], 3000)
        self.assertEqual(s["refunds_total"], "30.00")
        self.assertEqual(s["net_sales_cents"], 4000)
        self.assertEqual(s["net_sales"], "40.00")
        # completed_sales = value of live (non-failed, non-cancelled) orders:
        # ok1 $10 + ok2 $20 + partial order $20 (its $10 refund shows up in
        # net_sales = 70 − 30 = 40, not by shrinking completed_sales)
        self.assertEqual(s["completed_sales_cents"], 5000)
        self.assertEqual(s["avg_order_value_cents"], 1400)
        self.assertEqual(s["avg_order_value"], "14.00")
        self.assertEqual(s["currency"], "USDT")

        # top product is the only product
        self.assertEqual(s["top_products"][0]["product_id"], self.puba)
        self.assertEqual(s["top_products"][0]["units"], 7)   # all units incl. failed
        # orders_by_date covers today
        self.assertTrue(s["orders_by_date"])
        self.assertEqual(sum(d["orders"] for d in s["orders_by_date"]), 5)

    def test_reseller_b_isolation(self):
        r = self.client.get("/api/v1/analytics/sales", headers=_hdr(self.kb, self.sb))
        s = r.get_json()["data"]["sales"]
        self.assertEqual(s["orders_total"], 1)
        self.assertEqual(s["gross_sales"], "30.00")
        self.assertEqual(s["refunds_total_cents"], 0)
        # no trace of A's numbers
        self.assertEqual(s["quantity_sold"], 2)

    def test_owner_platform_view_and_filters(self):
        import web_admin.app as wa
        wa.app.config["TESTING"] = True
        wa.DB_PATH = _db_path
        ac = wa.app.test_client()
        with ac.session_transaction() as sess:
            sess["logged_in"] = True
            sess["username"] = "owner"

        # all resellers
        r = ac.get("/api/monitoring/sales-analytics")
        self.assertEqual(r.status_code, 200)
        s = r.get_json()["sales"]
        self.assertEqual(s["orders_total"], 6)
        self.assertEqual(s["gross_sales"], "100.00")   # 70 + 30
        self.assertEqual(s["refunds_total"], "30.00")
        self.assertEqual(s["net_sales"], "70.00")
        board = {b["reseller_id"]: b for b in s["resellers"]}
        self.assertIn(self.ra["public_id"], board)
        self.assertIn(self.rb["public_id"], board)
        self.assertEqual(board[self.ra["public_id"]]["orders"], 5)
        self.assertEqual(board[self.rb["public_id"]]["gross_sales"], "30.00")

        # individual reseller filter
        r = ac.get(f"/api/monitoring/sales-analytics?reseller={self.ra['public_id']}")
        s = r.get_json()["sales"]
        self.assertEqual(s["orders_total"], 5)
        self.assertEqual(s["gross_sales"], "70.00")

        # product filter
        r = ac.get(f"/api/monitoring/sales-analytics?product={self.pubb}")
        s = r.get_json()["sales"]
        self.assertEqual(s["orders_total"], 1)
        self.assertEqual(s["gross_sales"], "30.00")

        # date range: future window -> empty
        r = ac.get("/api/monitoring/sales-analytics?date_from=2999-01-01&date_to=2999-12-31")
        s = r.get_json()["sales"]
        self.assertEqual(s["orders_total"], 0)
        self.assertEqual(s["gross_sales_cents"], 0)

        # owner gate
        anon = wa.app.test_client()
        self.assertEqual(anon.get("/api/monitoring/sales-analytics").status_code, 401)

    def test_date_filter_reseller_view(self):
        from utils.timezone import now as _now
        today = _now().strftime("%Y-%m-%d")
        r = self.client.get(f"/api/v1/analytics/sales?date_from={today}&date_to={today}",
                            headers=_hdr(self.ka, self.sa))
        s = r.get_json()["data"]["sales"]
        self.assertEqual(s["orders_total"], 5)
        r = self.client.get("/api/v1/analytics/sales?date_from=2999-01-01",
                            headers=_hdr(self.ka, self.sa))
        self.assertEqual(r.get_json()["data"]["sales"]["orders_total"], 0)
        # bad date format -> 400
        r = self.client.get("/api/v1/analytics/sales?date_from=last-week",
                            headers=_hdr(self.ka, self.sa))
        self.assertEqual(r.status_code, 400)

    def test_no_money_from_client_fields_and_privacy(self):
        """The analytics read only authoritative records; response carries no
        internal ids, secrets, or customer data."""
        import json as _json
        r = self.client.get("/api/v1/analytics/sales", headers=_hdr(self.ka, self.sa))
        flat = _json.dumps(r.get_json()).lower()
        for banned in ("secret", "user_id", "telegram", "reseller_id",
                       "unit_price_cents", "supplier", "cost"):
            self.assertNotIn(banned, flat)

    def test_index_bounded_queries(self):
        db.cur.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM orders o"
            " WHERE o.reseller_id IS NOT NULL AND o.source='reseller_api'"
            " AND o.reseller_id=? AND o.created >= ?", (1, "2026-01-01"))
        plan = " ".join(str(r[-1]) for r in db.cur.fetchall())
        self.assertIn("idx_orders_reseller", plan)


if __name__ == "__main__":
    unittest.main()
