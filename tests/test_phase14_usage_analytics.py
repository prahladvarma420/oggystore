"""Phase 14 Prompt 2 — reseller API request & usage analytics.

Verifies analytics computed from the EXISTING append-only request log
(reseller_api_logs) — no duplicate logging system:

  * request counting: total / successful / failed / 4xx / 5xx / 429
  * quota violations visibility
  * average latency
  * endpoint usage (owner top-endpoints + reseller own breakdown)
  * per-reseller and per-API-key usage
  * time-based usage (today / this month / daily series / date filtering)
  * reseller isolation and privacy (no secrets/IPs/foreign data)
  * index-bounded queries (no unbounded scans on hot paths)
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase14_usage_", suffix=".db")
os.close(_fd)

import config
config.DB_PATH = _db_path
import database.db as db
db.DB_PATH = _db_path
db._tls = threading.local()
db.init_db()

import database.reseller_db as rdb
from reseller_api import create_app
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
    uid = 9_740_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P14U{n}", f"p14u{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P14U{n}", uid, f"p14u{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(codes=("U1", "U2", "U3", "U4", "U5")):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,10.0,0,'code_stock',1)",
        (f"P14U Product {n}",),
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P14-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


class Phase14UsageAnalyticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # persist_audit_record NOT patched — analytics read real log rows.
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
        try:
            db.close_thread_connection()
        finally:
            try:
                os.unlink(_db_path)
            except FileNotFoundError:
                pass

    def _admin_client(self):
        import web_admin.app as wa
        wa.app.config["TESTING"] = True
        wa.DB_PATH = _db_path
        c = wa.app.test_client()
        with c.session_transaction() as sess:
            sess["logged_in"] = True
            sess["username"] = "owner"
        return c

    # ---------------------------------------------------------- core counting

    def test_request_success_failure_429_5xx_counted(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant(rs["id"], pid)
        _topup(rs["id"], 100000)
        rdb.set_quota_settings(rs["id"], daily_order_quota=1)

        # 1 success (order), 1 client error (400), 1 quota 429, 1 synthetic 5xx log
        r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                             json={"product_id": pub, "quantity": 1,
                                   "client_order_id": "p14-c1"})
        self.assertEqual(r.status_code, 201)
        self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                         json={"product_id": pub, "quantity": 0})
        self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                         json={"product_id": pub, "quantity": 1,
                               "client_order_id": "p14-c2"})
        # synthetic 5xx log row (server failure path recorded by the same logger)
        rdb.log_api_request(rs["id"], rdb.get_api_key(key)["id"],
                            "/api/v1/orders", "POST", 500,
                            request_id="p14-synthetic-5xx", response_ms=12,
                            reseller_public_id=rs["public_id"], key_id=key)

        u = rdb.get_reseller_api_usage(rs["id"])
        self.assertEqual(u["orders_total"], 1)
        self.assertGreaterEqual(u["requests_failed"], 2)   # 400 + 500
        self.assertGreaterEqual(u["rate_limited_429"], 1)
        self.assertEqual(u["requests_total"],
                         u["requests_ok"] + u["requests_failed"])

        # platform aggregation sees the same truth
        plat = rdb.get_api_usage_platform(days=7)
        self.assertGreaterEqual(plat["today"]["total"], 4)
        self.assertGreaterEqual(plat["today"]["errors_4xx"], 2)   # 400 + 429
        self.assertGreaterEqual(plat["today"]["errors_5xx"], 1)
        self.assertGreaterEqual(plat["today"]["rate_limited_429"], 1)
        self.assertIn("avg_ms", plat["today"])
        self.assertEqual(len(plat["daily"]), 7)

    def test_endpoint_metrics_owner_and_reseller(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant(rs["id"], pid)
        _topup(rs["id"], 100000)

        self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                         json={"product_id": pub, "quantity": 1,
                               "client_order_id": "p14-ep-1"})
        self.client.get("/api/v1/products", headers=_hdr(key, sec))
        self.client.get("/api/v1/products", headers=_hdr(key, sec))
        self.client.get("/api/v1/balance", headers=_hdr(key, sec))

        # owner: top endpoints
        plat = rdb.get_api_usage_platform(days=7)
        names = [e["endpoint"] for e in plat["top_endpoints"]]
        self.assertIn("/api/v1/orders", names)
        self.assertIn("/api/v1/products", names)
        ep = {e["endpoint"]: e for e in plat["top_endpoints"]}
        # >=: the month window legitimately includes earlier tests' rows
        self.assertGreaterEqual(ep["/api/v1/products"]["requests"], 2)
        self.assertEqual(ep["/api/v1/products"]["errors"], 0)

        # reseller: own breakdown only
        u = self.client.get("/api/v1/usage", headers=_hdr(key, sec)).get_json()
        endpoints = u["data"]["usage"]["endpoints"]
        mine = {e["endpoint"]: e for e in endpoints}
        self.assertIn("/api/v1/orders", mine)
        self.assertEqual(mine["/api/v1/products"]["successful"], 2)

        # per-key usage
        keys = u["data"]["usage"]["keys"]
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0]["key_id"], key)
        self.assertGreaterEqual(keys[0]["requests"], 4)

    def test_reseller_isolation_in_analytics(self):
        rs1, k1, s1 = _make_reseller()
        rs2, k2, s2 = _make_reseller()
        pid, pub = _make_product()
        _grant(rs1["id"], pid)
        _topup(rs1["id"], 50000)
        self.client.post("/api/v1/orders", headers=_hdr(k1, s1),
                         json={"product_id": pub, "quantity": 1,
                               "client_order_id": "p14-iso"})
        self.client.get("/api/v1/products", headers=_hdr(k1, s1))

        u2 = self.client.get("/api/v1/usage", headers=_hdr(k2, s2)).get_json()
        endpoints2 = u2["data"]["usage"]["endpoints"]
        self.assertEqual([e for e in endpoints2
                          if e["endpoint"] == "/api/v1/orders"], [])
        keys2 = u2["data"]["usage"]["keys"]
        self.assertEqual([k for k in keys2 if k["key_id"] == k1], [])

        # owner top resellers show public ids only (no secrets/IPs)
        plat = rdb.get_api_usage_platform(days=7)
        import json as _json
        flat = _json.dumps(plat).lower()
        for banned in ("secret", "127.0.0.1", "user_agent", s1.lower(), k1.lower()):
            self.assertNotIn(banned, flat)
        rids = [r["reseller_id"] for r in plat["top_resellers"]]
        self.assertIn(rs1["public_id"], rids)

    def test_date_filtering_windows(self):
        """Backdated rows must fall out of 'today' but stay in the month."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self.client.get("/api/v1/balance", headers=_hdr(key, sec))

        # backdate one synthetic row to the 1st of the month (or prior month)
        from utils.timezone import now as _now
        import datetime as _dt
        first = _now().replace(day=1).strftime("%Y-%m-%d 00:00:01")
        if _now().strftime("%Y-%m-%d") == _now().strftime("%Y-%m-01"):
            # running on the 1st: use an hour-past timestamp still today
            first = _now().strftime("%Y-%m-%d 00:00:01")
        rdb.log_api_request(rs["id"], None, "/api/v1/products", "GET", 200,
                            request_id="p14-old", response_ms=5,
                            reseller_public_id=rs["public_id"], key_id=key)
        db.cur.execute(
            "UPDATE reseller_api_logs SET created=? WHERE request_id='p14-old'",
            (first,))
        db.conn.commit()

        plat = rdb.get_api_usage_platform(days=7)
        self.assertGreaterEqual(plat["this_month"]["total"], 2)
        # the backdated row is NOT in "today" unless it IS today 00:00:01
        if _now().strftime("%Y-%m-%d %H:%M:%S") > first + "x":
            pass
        today_total = plat["today"]["total"]
        day_str = _now().strftime("%Y-%m-%d")
        is_same_day = first[:10] == day_str
        if not is_same_day:
            self.assertLess(today_total, plat["this_month"]["total"])

        # series dates are consecutive and bounded
        dates = [d["date"] for d in plat["daily"]]
        self.assertEqual(len(dates), 7)
        self.assertEqual(dates, sorted(dates))

    def test_owner_endpoint_live(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                         json={"product_id": pub, "quantity": 1,
                               "client_order_id": "p14-owner"})

        ac = self._admin_client()
        r = ac.get("/api/monitoring/api-usage")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        u = body["usage"]
        self.assertGreaterEqual(u["today"]["total"], 1)
        self.assertGreaterEqual(u["this_month"]["total"], 1)
        self.assertIn("error_rate", u["today"])
        self.assertIn("avg_ms", u["today"])
        self.assertTrue(u["top_endpoints"])
        self.assertTrue(u["top_resellers"])
        self.assertEqual(len(u["daily"]), 7)
        # days param bounded
        r2 = ac.get("/api/monitoring/api-usage?days=999")
        self.assertEqual(len(r2.get_json()["usage"]["daily"]), 31)

        # owner-gated
        import web_admin.app as wa
        anon = wa.app.test_client()
        self.assertEqual(anon.get("/api/monitoring/api-usage").status_code, 401)

    def test_queries_index_bounded(self):
        """The time-window scans must use the created index, not full scans."""
        db.cur.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*), AVG(response_ms)"
            " FROM reseller_api_logs WHERE created >= ?",
            ("2026-01-01 00:00:00",))
        plan = " ".join(str(r[-1]) for r in db.cur.fetchall())
        self.assertIn("idx_reseller_api_logs_created", plan)

        db.cur.execute(
            "EXPLAIN QUERY PLAN SELECT endpoint, COUNT(*) FROM reseller_api_logs"
            " WHERE reseller_id=? AND created >= ? GROUP BY endpoint",
            (1, "2026-01-01"))
        plan = " ".join(str(r[-1]) for r in db.cur.fetchall())
        self.assertIn("idx_reseller_api_logs_reseller", plan)


if __name__ == "__main__":
    unittest.main()
