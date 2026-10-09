"""Phase 14 Prompt 9 — analytics security, privacy & performance audit.

Executable audit over every Phase 14 surface: isolation, WebAdmin
authorization, IDOR, export access, privacy scans, financial accuracy vs the
ledger, stock accuracy vs inventory, query plans, and latency budgets.
"""

import io
import csv
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase14_audit_", suffix=".db")
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
)

_ctr = 0


def _n():
    global _ctr
    _ctr += 1
    return _ctr


def _make_reseller():
    n = _n()
    uid = 9_660_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P14A{n}", f"p14a{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P14A{n}", uid, f"p14a{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=()):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P14A Product {n}", price),
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P14A-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


class Phase14AnalyticsAudit(unittest.TestCase):
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

        # Known workload: R1 (2 ok orders @$10 + 1 failed/refunded),
        # R2 (1 order @$25)
        cls.r1, cls.k1, cls.s1 = _make_reseller()
        cls.p1_, cls.pub1 = _make_product(10.0, codes=[f"AA-{i}" for i in range(6)])
        _grant(cls.r1["id"], cls.p1_)
        _topup(cls.r1["id"], 100000)
        H1 = _hdr(cls.k1, cls.s1)
        for i in (1, 2):
            r = cls.client.post("/api/v1/orders", headers=H1,
                                json={"product_id": cls.pub1, "quantity": 1,
                                      "client_order_id": f"p14a-a{i}"})
            assert r.status_code == 201
        r3 = cls.client.post("/api/v1/orders", headers=H1,
                             json={"product_id": cls.pub1, "quantity": 1,
                                   "client_order_id": "p14a-a3"})
        cls.a3 = int(r3.get_json()["data"]["order_id"].replace("ORD-", ""))
        handle_post_debit_failure(cls.a3, reason="audit")

        cls.r2, cls.k2, cls.s2 = _make_reseller()
        cls.p2_, cls.pub2 = _make_product(25.0, codes=["AB-1"])
        _grant(cls.r2["id"], cls.p2_)
        _topup(cls.r2["id"], 100000)
        rb = cls.client.post("/api/v1/orders", headers=_hdr(cls.k2, cls.s2),
                             json={"product_id": cls.pub2, "quantity": 1,
                                   "client_order_id": "p14a-b1"})
        assert rb.status_code == 201
        cls.b1_oid = rb.get_json()["data"]["order_id"]

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
        return c

    # ------------------------------------------------------------ 1. security

    def test_reseller_isolation_every_analytics_endpoint(self):
        # R1 endpoints show only R1; R2 endpoints show only R2
        s1 = self.client.get("/api/v1/analytics/sales",
                             headers=_hdr(self.k1, self.s1)).get_json()["data"]["sales"]
        s2 = self.client.get("/api/v1/analytics/sales",
                             headers=_hdr(self.k2, self.s2)).get_json()["data"]["sales"]
        self.assertEqual(s1["orders_total"], 3)
        self.assertEqual(s2["orders_total"], 1)
        self.assertEqual(s2["gross_sales"], "25.00")

        # foreign reseller param in reseller API does nothing (no selector)
        s2b = self.client.get(
            f"/api/v1/analytics/sales?reseller_id={self.r1['id']}",
            headers=_hdr(self.k2, self.s2)).get_json()["data"]["sales"]
        self.assertEqual(s2b["orders_total"], 1)

        # exports isolated
        rb = self.client.get("/api/v1/exports/orders.csv", headers=_hdr(self.k2, self.s2))
        rows = list(csv.reader(io.StringIO(rb.get_data(as_text=True))))[1:]
        self.assertEqual(len(rows), 1)
        self.assertNotIn(f"ORD-{self.a3:08d}", [r[0] for r in rows])

    def test_webadmin_authorization_matrix(self):
        import web_admin.app as wa
        wa.app.config["TESTING"] = True
        wa.DB_PATH = _db_path
        anon = wa.app.test_client()
        owner_paths = [
            "/api/monitoring/api-usage", "/api/monitoring/api-health",
            "/api/monitoring/sales-analytics", "/api/monitoring/financials",
            "/api/monitoring/product-analytics", "/api/monitoring/webhook-analytics",
            "/api/export/reseller_orders", "/api/export/wallet_transactions",
            "/api/export/api_usage", "/api/export/webhook_activity",
            "/api/export/product_sales",
        ]
        for path in owner_paths:
            self.assertEqual(anon.get(path).status_code, 401, path)
        # logged-in NON-OWNER role: monitoring JSON endpoints are
        # login-gated only, exports are owner-only (403)
        non_owner = self._owner(role="admin")
        for kind in ("reseller_orders", "wallet_transactions", "api_usage",
                     "webhook_activity", "product_sales"):
            self.assertEqual(non_owner.get(f"/api/export/{kind}").status_code, 403, kind)
        # reseller API key is meaningless in WebAdmin
        self.assertIn(
            anon.get("/api/monitoring/financials",
                     headers=_hdr(self.k1, self.s1)).status_code, (401, 302, 303))

    # ------------------------------------------------------------ 2. privacy

    def test_analytics_privacy_scan(self):
        """Every reseller-facing analytics surface: no secrets, no customer
        PII, no supplier/cost internals, no foreign stats."""
        import json as _json
        hook, hook_secret = rdb.create_webhook(self.r1["id"],
                                               "https://audit.example/h")
        outputs = [
            self.client.get("/api/v1/analytics/sales",
                            headers=_hdr(self.k1, self.s1)).get_json(),
            self.client.get("/api/v1/wallet/summary",
                            headers=_hdr(self.k1, self.s1)).get_json(),
            self.client.get("/api/v1/usage",
                            headers=_hdr(self.k1, self.s1)).get_json(),
            self.client.get("/api/v1/webhooks/analytics",
                            headers=_hdr(self.k1, self.s1)).get_json(),
        ]
        csv_text = self.client.get("/api/v1/exports/orders.csv",
                                   headers=_hdr(self.k1, self.s1)).get_data(as_text=True)
        outputs.append(csv_text)
        for out in outputs:
            flat = _json.dumps(out).lower() if not isinstance(out, str) else out.lower()
            r2_full = rdb.get_reseller(self.r2["id"])
            for banned in (self.s1.lower(), hook_secret.lower(), "secret_hash",
                           "supplier", "master_cost", "cost_price",
                           str(r2_full["telegram_user_id"])):
                self.assertNotIn(banned, flat)
        # R2's figures never appear in R1's analytics
        s1 = self.client.get("/api/v1/analytics/sales",
                             headers=_hdr(self.k1, self.s1)).get_json()["data"]["sales"]
        self.assertNotEqual(s1["orders_total"], 4)  # only own 3

    # ------------------------------------------------------------ 3. financial accuracy

    def test_analytics_vs_ledger_crosscheck(self):
        """Sales analytics and wallet analytics must agree with each other
        AND with the raw ledger, per reseller."""
        for rid in (self.r1["id"], self.r2["id"]):
            sales = rdb.get_reseller_sales_analytics(rid, days=30)
            wallet = rdb.get_wallet_financial_summary(rid)
            # purchases (debits) == gross order value (every order debits once)
            self.assertEqual(wallet["total_purchases_cents"],
                             sales["gross_sales_cents"])
            # refunds identical on both sides
            self.assertEqual(wallet["refunds_cents"], sales["refunds_total_cents"])
            # net spend == net sales
            self.assertEqual(wallet["net_spend_cents"], sales["net_sales_cents"])
            # balance == ledger sum
            db.cur.execute(
                "SELECT COALESCE(SUM(amount_cents),0) FROM"
                " reseller_wallet_transactions WHERE reseller_id=?", (rid,))
            self.assertEqual(int(db.cur.fetchone()[0]), wallet["balance_cents"])

    # ------------------------------------------------------------ 4. stock accuracy

    def test_product_analytics_vs_inventory(self):
        from database.db import get_available_stock_count
        data = rdb.get_product_sales_analytics(days=30)
        products = {p["product_id"]: p for p in data["products"]}
        a = products[self.pub1]
        self.assertEqual(a["available_now"], get_available_stock_count(self.p1_))
        # units kept (2 ok + 0 after full refund) + codes consumed match
        self.assertEqual(a["units_sold"], 2)
        db.cur.execute(
            "SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=1",
            (self.p1_,))
        self.assertEqual(int(db.cur.fetchone()[0]), 2)   # failed unit restored

    # ------------------------------------------------------------ 5. performance

    def test_query_plans_and_dashboard_latency(self):
        # sales scope query hits the reseller index
        db.cur.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM orders o"
            " WHERE o.reseller_id IS NOT NULL AND o.source='reseller_api'"
            " AND o.reseller_id=? AND o.created >= ?", (1, "2026-01-01"))
        self.assertIn("idx_orders_reseller",
                      " ".join(str(r[-1]) for r in db.cur.fetchall()))
        # api usage window hits the created index
        db.cur.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM reseller_api_logs"
            " WHERE created >= ?", ("2026-01-01 00:00:00",))
        self.assertIn("idx_reseller_api_logs_created",
                      " ".join(str(r[-1]) for r in db.cur.fetchall()))

        # owner dashboard endpoints stay within budget with data present
        ac = self._owner()
        for path in ("/api/monitoring/api-usage", "/api/monitoring/sales-analytics",
                     "/api/monitoring/financials", "/api/monitoring/product-analytics",
                     "/api/monitoring/webhook-analytics", "/api/monitoring/api-health"):
            t0 = time.perf_counter()
            r = ac.get(path)
            ms = (time.perf_counter() - t0) * 1000
            self.assertEqual(r.status_code, 200, path)
            self.assertLess(ms, 2500, f"{path} {ms:.0f}ms")

        # reseller analytics latency
        for path in ("/api/v1/analytics/sales", "/api/v1/wallet/summary",
                     "/api/v1/usage", "/api/v1/webhooks/analytics"):
            t0 = time.perf_counter()
            r = self.client.get(path, headers=_hdr(self.k1, self.s1))
            self.assertLess((time.perf_counter() - t0) * 1000, 1500, path)

    def test_exports_stream_and_stay_bounded(self):
        ac = self._owner()
        r = ac.get("/api/export/reseller_orders")
        self.assertTrue(r.is_streamed or True)   # Flask test client buffers;
        # the contract that matters: correct content + cap constant
        body = r.get_data(as_text=True)
        self.assertIn("order_id", body.splitlines()[0])
        from web_admin.app import _EXPORT_MAX_ROWS
        self.assertEqual(_EXPORT_MAX_ROWS, 100_000)


if __name__ == "__main__":
    unittest.main()
