"""Phase 14 Prompt 5 — product sales, stock & inventory analytics.

Verified against a known multi-product, multi-reseller workload with real
orders, a partial refund (restored unit), a disabled product, an
out-of-stock product, and an archived product.
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase14_prod_", suffix=".db")
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
    handle_partial_failure,
)

_ctr = 0


def _n():
    global _ctr
    _ctr += 1
    return _ctr


def _make_reseller():
    n = _n()
    uid = 9_710_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P14P{n}", f"p14p{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P14P{n}", uid, f"p14p{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price, name, codes=(), category=None, manual=0, active=1):
    n = _n()
    stock = 0 if codes else int(manual)
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,?,'code_stock',?)",
        (name, price, stock, active),
    )
    db.conn.commit()
    pid = db.cur.lastrowid
    db.cur.execute("UPDATE products SET max_per_order=25 WHERE id=?", (pid,))
    if category:
        db.cur.execute("INSERT INTO categories(name) VALUES(?)", (category,))
        db.conn.commit()
        db.cur.execute("UPDATE products SET category_id=? WHERE id=?",
                       (db.cur.lastrowid, pid))
        db.conn.commit()
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P14P-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


def _order(client, key, sec, pub, qty, coid):
    r = client.post("/api/v1/orders", headers=_hdr(key, sec),
                    json={"product_id": pub, "quantity": qty,
                          "client_order_id": coid})
    assert r.status_code == 201, r.get_json()
    return int(r.get_json()["data"]["order_id"].replace("ORD-", ""))


class Phase14ProductAnalyticsTests(unittest.TestCase):
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

        # ---- known workload ------------------------------------------
        # Product A (category "Streaming", $10): R1 orders 2 (ok),
        #   R1 orders 2 -> 1 partial-refunded (kept 1), R2 orders 1 (ok)
        cls.pa, cls.puba = _make_product(10.0, "P14 Stream A",
                                         codes=[f"A-{i}" for i in range(8)],
                                         category="Streaming")
        # Product B (category "Software", $25): R2 order fails -> refunded
        cls.pb, cls.pubb = _make_product(25.0, "P14 Soft B", codes=["B-1", "B-2"],
                                         category="Software")
        # Product C: active, adopted, but zero sales (low performer)
        cls.pc, cls.pubc = _make_product(5.0, "P14 Quiet C", codes=["C-1"])
        # Product D: sold out (out of stock now)
        cls.pd, cls.pubd = _make_product(7.0, "P14 Gone D", codes=["D-1"])
        # Product E: disabled after import
        cls.pe, cls.pube = _make_product(9.0, "P14 Disabled E", codes=["E-1"])
        # Product F: archived (active=0 before any adoption)
        cls.pf, cls.pubf = _make_product(4.0, "P14 Archived F", codes=["F-1"],
                                         active=0)

        cls.r1, cls.k1, cls.s1 = _make_reseller()
        cls.r2, cls.k2, cls.s2 = _make_reseller()
        for pid in (cls.pa, cls.pb, cls.pc, cls.pd, cls.pe):
            _grant(cls.r1["id"], pid)
            _grant(cls.r2["id"], pid)
        _topup(cls.r1["id"], 200000)
        _topup(cls.r2["id"], 200000)

        _order(cls.client, cls.k1, cls.s1, cls.puba, 2, "p14p-a1")   # A: 2 ok
        pa_part = _order(cls.client, cls.k1, cls.s1, cls.puba, 2, "p14p-a2")
        handle_partial_failure(pa_part, fulfilled_qty=1, failed_qty=1)  # keep 1
        _order(cls.client, cls.k2, cls.s2, cls.puba, 1, "p14p-a3")   # A: 1 ok (R2)

        pb_fail = _order(cls.client, cls.k2, cls.s2, cls.pubb, 1, "p14p-b1")
        handle_post_debit_failure(pb_fail, reason="test")            # B failed

        _order(cls.client, cls.k1, cls.s1, cls.pubd, 1, "p14p-d1")   # D sells out

        # disable E after imports (owner action)
        db.cur.execute("UPDATE products SET active=0 WHERE id=?", (cls.pe,))
        db.conn.commit()

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
        return c

    # ------------------------------------------------------------- owner view

    def test_owner_product_analytics_exact(self):
        r = self._owner().get("/api/monitoring/product-analytics?days=30")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        data = body["products"]
        products = {p["product_id"]: p for p in data["products"]}

        a = products[self.puba]
        self.assertEqual(a["orders"], 3)                # a1 + a2 + a3
        self.assertEqual(a["successful_orders"], 3)
        self.assertEqual(a["failed_orders"], 0)
        self.assertEqual(a["units_sold"], 4)            # 2 + (2-1 refunded) + 1
        self.assertEqual(a["resellers"], 2)             # R1 and R2
        self.assertEqual(a["adopted_by"], 2)            # both imported it
        self.assertEqual(a["gross_sales"], "50.00")     # 5 units x $10 gross
        self.assertEqual(a["category"], "Streaming")

        b = products[self.pubb]
        self.assertEqual(b["orders"], 1)
        self.assertEqual(b["successful_orders"], 0)
        self.assertEqual(b["failed_orders"], 1)
        self.assertEqual(b["units_sold"], 0)            # failed: nothing kept
        self.assertEqual(b["gross_sales"], "25.00")

        # low performer: adopted but zero sales
        c = products[self.pubc]
        self.assertEqual(c["orders"], 0)
        self.assertEqual(c["adopted_by"], 2)
        low_ids = [p["product_id"] for p in data["low_performers"]]
        self.assertIn(self.pubc, low_ids)

        # sold out: current availability 0 + stockout flag
        d = products[self.pubd]
        self.assertEqual(d["available_now"], 0)
        self.assertTrue(d["out_of_stock"])
        self.assertEqual(d["stock_consumed_codes"], 1)

        # disabled product still reports (history intact), active flag false
        e = products[self.pube]
        self.assertFalse(e["active"])
        # archived product present with zero sales
        f = products[self.pubf]
        self.assertFalse(f["active"])
        self.assertEqual(f["orders"], 0)

        # categories rollup
        cats = {c["category"]: c for c in data["categories"]}
        self.assertEqual(cats["Streaming"]["units_sold"], 4)
        self.assertEqual(cats["Streaming"]["orders"], 3)
        self.assertEqual(cats["Software"]["orders"], 1)
        self.assertIn("Uncategorized", cats)            # C, D, E, F

        self.assertGreaterEqual(data["out_of_stock_count"], 1)
        self.assertEqual(data["currency"], "USDT")

        # owner gate
        import web_admin.app as wa
        anon = wa.app.test_client()
        self.assertEqual(anon.get("/api/monitoring/product-analytics").status_code, 401)

    def test_days_window_bounds(self):
        r = self._owner().get("/api/monitoring/product-analytics?days=999")
        self.assertEqual(r.get_json()["products"]["window_days"], 90)

    # ------------------------------------------------------------- reseller view

    def test_reseller_product_drilldown(self):
        # unfiltered: R1's orders are A(x2, $20) + A partial ($20) + D ($7)
        r = self.client.get("/api/v1/analytics/sales", headers=_hdr(self.k1, self.s1))
        s = r.get_json()["data"]["sales"]
        self.assertEqual(s["orders_total"], 3)
        self.assertEqual(s["quantity_sold"], 4)         # 2 + 1 kept + 1 (D)
        self.assertEqual(s["gross_sales"], "47.00")

        # drilldown to product A
        r = self.client.get(f"/api/v1/analytics/sales?product_id={self.puba}",
                            headers=_hdr(self.k1, self.s1))
        s = r.get_json()["data"]["sales"]
        self.assertEqual(s["orders_total"], 2)
        self.assertEqual(s["refunds_partial_cents"], 1000)   # scoped refund
        self.assertEqual(s["net_sales_cents"], 3000)         # 4000-1000

        # drilldown to product B (R1 has access but never ordered) -> zeros
        r = self.client.get(f"/api/v1/analytics/sales?product_id={self.pubb}",
                            headers=_hdr(self.k1, self.s1))
        s = r.get_json()["data"]["sales"]
        self.assertEqual(s["orders_total"], 0)
        self.assertEqual(s["gross_sales_cents"], 0)
        self.assertEqual(s["refunds_total_cents"], 0)

        # malformed product id -> 400; inaccessible/unknown -> zeros (no oracle)
        r = self.client.get("/api/v1/analytics/sales?product_id=not-a-product",
                            headers=_hdr(self.k1, self.s1))
        self.assertEqual(r.status_code, 400)
        r = self.client.get("/api/v1/analytics/sales?product_id=PRD-AAAAAAAAAAAA",
                            headers=_hdr(self.k1, self.s1))
        self.assertEqual(r.get_json()["data"]["sales"]["orders_total"], 0)

    def test_reseller_drilldown_refund_scoping(self):
        """R2 has sales on A (ok) and B (failed+refunded).  Drilling into B
        must show B's refund only — never A's numbers."""
        r = self.client.get(f"/api/v1/analytics/sales?product_id={self.pubb}",
                            headers=_hdr(self.k2, self.s2))
        s = r.get_json()["data"]["sales"]
        self.assertEqual(s["orders_total"], 1)
        self.assertEqual(s["refunds_full_cents"], 2500)
        self.assertEqual(s["net_sales_cents"], 0)
        # and on A: only the A order
        r = self.client.get(f"/api/v1/analytics/sales?product_id={self.puba}",
                            headers=_hdr(self.k2, self.s2))
        s = r.get_json()["data"]["sales"]
        self.assertEqual(s["orders_total"], 1)
        self.assertEqual(s["refunds_total_cents"], 0)


if __name__ == "__main__":
    unittest.main()
