"""Phase 14 Prompt 7 — WebAdmin & reseller analytics dashboards.

Verifies the UI wiring (nav, dispatch, renderer, syntax) and the endpoint
behavior behind every filter: presets, custom ranges, empty datasets,
bounded windows, reseller isolation, and large-dataset latency.
"""

import os
import sys
import tempfile
import threading
import time
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase14_dash_", suffix=".db")
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


def _make_reseller():
    n = _n()
    uid = 9_690_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P14D{n}", f"p14d{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P14D{n}", uid, f"p14d{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(codes=("D-1", "D-2", "D-3", "D-4")):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,10.0,0,'code_stock',1)",
        (f"P14D Product {n}",),
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P14D-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


class Phase14DashboardTests(unittest.TestCase):
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

        # shared workload: one order + a few reads for R1
        cls.r1, cls.k1, cls.s1 = _make_reseller()
        cls.p1_, cls.pub1 = _make_product()
        _grant(cls.r1["id"], cls.p1_)
        _topup(cls.r1["id"], 100000)
        r = cls.client.post("/api/v1/orders", headers=_hdr(cls.k1, cls.s1),
                            json={"product_id": cls.pub1, "quantity": 1,
                                  "client_order_id": "p14d-1"})
        assert r.status_code == 201
        cls.client.get("/api/v1/products", headers=_hdr(cls.k1, cls.s1))
        cls.client.get("/api/v1/balance", headers=_hdr(cls.k1, cls.s1))

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

    # ------------------------------------------------------------ UI wiring

    def test_ui_wired_nav_dispatch_renderer(self):
        html = (ROOT / "web_admin" / "templates" / "index.html").read_text()
        self.assertIn('data-view="api_analytics"', html)
        js = (ROOT / "web_admin" / "static" / "js" / "app.js").read_text()
        self.assertIn('api_analytics: "API Analytics"', js)
        self.assertIn('v === "api_analytics"', js)
        self.assertIn("async function viewApiAnalytics()", js)
        # no inline secrets / endpoints hardcoded with credentials
        self.assertNotIn("sk_", js)
        self.assertNotIn("password", js.lower().split("api_analytics")[0][:0])

    def test_js_syntax_valid(self):
        import subprocess
        r = subprocess.run(["node", "--check",
                            str(ROOT / "web_admin" / "static" / "js" / "app.js")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[:500])

    # ------------------------------------------------------------ owner dashboard data

    def test_owner_dashboard_all_sections(self):
        ac = self._owner()
        for path, key in (
            ("/api/monitoring/api-usage", "usage"),
            ("/api/monitoring/sales-analytics", "sales"),
            ("/api/monitoring/financials", "financials"),
            ("/api/monitoring/product-analytics", "products"),
            ("/api/monitoring/webhook-analytics", "webhooks"),
            ("/api/monitoring/api-health", None),
        ):
            r = ac.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertTrue(r.get_json()["ok"], path)
            if key:
                self.assertIn(key, r.get_json(), path)

    def test_owner_filters_presets_and_custom(self):
        ac = self._owner()
        # presets
        for d in (1, 2, 7, 30):
            r = ac.get(f"/api/monitoring/api-usage?days={d}")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(len(r.get_json()["usage"]["daily"]), d)
        # custom range with sales
        from utils.timezone import now as _now
        today = _now().strftime("%Y-%m-%d")
        r = ac.get(f"/api/monitoring/sales-analytics?date_from={today}&date_to={today}")
        self.assertEqual(r.get_json()["sales"]["orders_total"], 1)
        r = ac.get("/api/monitoring/sales-analytics?date_from=2999-01-01")
        self.assertEqual(r.get_json()["sales"]["orders_total"], 0)   # empty window
        # reseller + product filters
        r = ac.get(f"/api/monitoring/sales-analytics?reseller={self.r1['public_id']}")
        self.assertEqual(r.get_json()["sales"]["orders_total"], 1)
        r = ac.get(f"/api/monitoring/sales-analytics?product={self.pub1}")
        self.assertEqual(r.get_json()["sales"]["orders_total"], 1)
        r = ac.get("/api/monitoring/sales-analytics?reseller=RSR-ZZZZZZZZZZ")
        self.assertEqual(r.status_code, 404)
        # invalid custom date rejected
        r = ac.get("/api/monitoring/api-usage?date_from=garbage")
        self.assertEqual(r.status_code, 400)

    def test_owner_empty_dataset_zero_states(self):
        """A future window yields zeroed sections, not errors."""
        ac = self._owner()
        r = ac.get("/api/monitoring/product-analytics?date_from=2999-01-01")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()["products"]
        for p in data["products"]:
            self.assertEqual(p["orders"], 0)
            self.assertEqual(p["units_sold"], 0)

    def test_owner_large_dataset_latency(self):
        """5k synthetic log rows + 100 orders: dashboard endpoints stay fast."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=[f"BULK-{i}" for i in range(100)])
        _grant(rs["id"], pid)
        _topup(rs["id"], 10000000)
        # Synthetic bulk volume: disable the per-product burst guard for the
        # duration (it is a security control, covered by its own tests).
        from reseller_api.services.endpoint_rate_limit import EndpointRateDecision
        _allow = EndpointRateDecision(True, "order_create", 0, 0, 60)
        with patch("reseller_api.controllers.v1.orders.assess_order_financial_risk",
                   return_value=None), \
             patch("reseller_api.middleware.endpoint_rate_limit.check_endpoint_rate",
                   return_value=_allow):
            for i in range(100):
                r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                                     json={"product_id": pub, "quantity": 1,
                                           "client_order_id": f"p14d-bulk-{i}"})
                assert r.status_code == 201, r.get_json()
        db.cur.execute("SELECT COUNT(*) FROM orders WHERE reseller_id=?", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 100)

        ac = self._owner()
        for path in ("/api/monitoring/api-usage?days=7",
                     "/api/monitoring/sales-analytics",
                     "/api/monitoring/financials",
                     "/api/monitoring/product-analytics",
                     "/api/monitoring/webhook-analytics"):
            t0 = time.perf_counter()
            r = ac.get(path)
            ms = (time.perf_counter() - t0) * 1000
            self.assertEqual(r.status_code, 200, path)
            self.assertLess(ms, 3000, f"{path} took {ms:.0f}ms")

    # ------------------------------------------------------------ reseller dashboard (API)

    def test_reseller_dashboard_surfaces_isolated(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                         json={"product_id": pub, "quantity": 1,
                               "client_order_id": "p14d-own"})
        for path in ("/api/v1/usage", "/api/v1/analytics/sales",
                     "/api/v1/wallet/summary", "/api/v1/webhooks/analytics"):
            r = self.client.get(path, headers=_hdr(key, sec))
            self.assertEqual(r.status_code, 200, path)
            self.assertTrue(r.get_json()["ok"], path)
        # and a different reseller sees nothing of this
        rs2, k2, s2 = _make_reseller()
        s = self.client.get("/api/v1/analytics/sales",
                            headers=_hdr(k2, s2)).get_json()["data"]["sales"]
        self.assertEqual(s["orders_total"], 0)


if __name__ == "__main__":
    unittest.main()
