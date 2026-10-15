"""Phase 13 Prompt 8 — reliability, observability & incident handling.

Verifies the API stays diagnosable and fails safely:
  * request tracing: response request_id joins the persisted audit row
    (reseller, endpoint, method, status, latency, timestamp)
  * latency: per-endpoint budgets + response_ms persisted
  * dependency failure: DB down -> sanitized 500s with no financial side
    effects; /health/db -> 503 generic; /health -> always 200
  * slow database: requests still correct, latency captured
  * worker failure: events stay pending, financials unaffected
  * Redis: not part of the architecture (asserted, documented)
  * API restart: state survives, replays resolve
  * high request volume: stable, no 5xx
  * owner monitoring endpoint: spike counts, webhook failures, DB probe,
    login-gated
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase13_rel_", suffix=".db")
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
    uid = 9_770_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P13L{n}", f"p13l{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P13L{n}", uid, f"p13l{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(codes=("L-1", "L-2", "L-3", "L-4")):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,10.0,0,'code_stock',1)",
        (f"P13L Product {n}",),
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P13L-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


class Phase13ReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # persist_audit_record NOT patched — tracing assertions need real rows.
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

    # ---------------------------------------------------------- tracing

    def test_request_tracing_end_to_end(self):
        rs, key, sec = _make_reseller()
        r = self.client.get("/api/v1/products", headers=_hdr(key, sec))
        self.assertEqual(r.status_code, 200)
        rid = r.get_json()["request_id"]
        self.assertTrue(rid)
        db.cur.execute(
            "SELECT reseller_id, endpoint, method, status_code, response_ms,"
            " created, reseller_public_id, key_id"
            " FROM reseller_api_logs WHERE request_id=?", (rid,))
        row = db.cur.fetchone()
        self.assertIsNotNone(row, "request_id must join the audit log")
        (reseller_id, endpoint, method, status, ms, created, rpub, kid) = row
        self.assertEqual(reseller_id, rs["id"])
        self.assertEqual(reseller_id and True, True)
        self.assertEqual(endpoint, "/api/v1/products")
        self.assertEqual(method, "GET")
        self.assertEqual(status, 200)
        self.assertIsNotNone(ms)
        self.assertTrue(created)
        self.assertEqual(rpub, rs["public_id"])
        self.assertEqual(kid, key)

    # ---------------------------------------------------------- latency

    def test_latency_budgets_and_measurement(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant(rs["id"], pid)
        _topup(rs["id"], 100000)
        rdb.create_webhook(rs["id"], "https://lat.example/h")

        budgets = [
            ("GET",  "/api/v1/products", None, 500),
            ("GET",  f"/api/v1/products/{pub}", None, 500),
            ("POST", "/api/v1/orders/quote",
             {"product_id": pub, "quantity": 1}, 500),
            ("POST", "/api/v1/orders",
             {"product_id": pub, "quantity": 1, "client_order_id": "p13l-lat"}, 750),
            ("GET",  "/api/v1/orders", None, 500),
            ("GET",  "/api/v1/webhooks", None, 500),
        ]
        order_id = None
        for method, path, body, budget_ms in budgets:
            t0 = time.perf_counter()
            resp = (self.client.post if method == "POST" else self.client.get)(
                path, headers=_hdr(key, sec), **({"json": body} if body else {}))
            elapsed = (time.perf_counter() - t0) * 1000
            self.assertIn(resp.status_code, (200, 201), path)
            self.assertLess(elapsed, budget_ms,
                            f"{path} took {elapsed:.0f}ms (budget {budget_ms}ms)")
            if method == "POST" and path == "/api/v1/orders":
                order_id = resp.get_json()["data"]["order_id"]
        # delivery retrieval
        t0 = time.perf_counter()
        resp = self.client.get(f"/api/v1/orders/{order_id}/delivery",
                               headers=_hdr(key, sec))
        self.assertLess((time.perf_counter() - t0) * 1000, 500)
        # every call above got a latency row
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_api_logs WHERE reseller_id=?"
            " AND response_ms IS NOT NULL", (rs["id"],))
        self.assertGreaterEqual(int(db.cur.fetchone()[0]), len(budgets) + 1)

    # ---------------------------------------------------------- dependency failure

    def test_database_failure_fails_closed_and_sanitized(self):
        import reseller_api.services.order_service as osvc

        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["DBF-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)

        class ExplodingCursor:
            def execute(self, sql, params=()):
                if "INSERT INTO orders" in str(sql):
                    raise RuntimeError("database is locked: /var/lib/prod.db")
                return db.get_cursor().execute(sql, params)

            def __getattr__(self, name):
                return getattr(db.get_cursor(), name)

        with patch.object(osvc, "get_cursor", lambda: ExplodingCursor()):
            r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                                 json={"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 500)
        body = r.get_json()
        self.assertEqual(body["error"]["code"], "order_creation_failed")
        flat = str(body).lower()
        for leak in ("locked", "/var/lib", "sqlite", ".db"):
            self.assertNotIn(leak, flat)
        # fails closed financially: no charge, no order, stock intact
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)
        db.cur.execute("SELECT COUNT(*) FROM orders WHERE reseller_id=?", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 0)
        db.cur.execute(
            "SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=1", (pid,))
        self.assertEqual(int(db.cur.fetchone()[0]), 0)

    def test_health_endpoints_under_db_failure(self):
        # liveness never touches the DB
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "ok")

        # readiness fails generic 503 without internals
        import database.db as _dbmod

        class BrokenConn:
            def cursor(self):
                raise RuntimeError("cant connect tcp://db.internal:5432")

        with patch.object(_dbmod, "get_conn", side_effect=lambda *a, **k: BrokenConn()):
            r = self.client.get("/health/db")
        self.assertEqual(r.status_code, 503)
        self.assertNotIn("tcp", str(r.get_json()).lower())
        self.assertNotIn("internal", str(r.get_json()).lower())

    def test_slow_database_still_correct_and_measured(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["SLOW-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)

        real_conn = db.get_conn()

        class SlowCursor:
            def __init__(self, c):
                self._c = c

            def execute(self, *a, **k):
                time.sleep(0.03)
                return self._c.execute(*a, **k)

            def __getattr__(self, name):
                return getattr(self._c, name)

        class SlowConn:
            def cursor(self):
                return SlowCursor(real_conn.cursor())

            def __getattr__(self, name):
                return getattr(real_conn, name)

        # The services call get_cursor() bound into THEIR namespaces
        # (from-imports), so patch at the reseller_db boundary the balance
        # and auth paths actually use.
        t0 = time.perf_counter()
        with patch.object(rdb, "get_cursor",
                          lambda: SlowCursor(real_conn.cursor())):
            r = self.client.get("/api/v1/balance", headers=_hdr(key, sec))
        elapsed = (time.perf_counter() - t0) * 1000
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["data"]["balance"], "500.00")
        self.assertGreater(elapsed, 25)   # the slowdown is visible, not fatal

    def test_worker_failure_keeps_state_consistent(self):
        """Delivery worker down: the order commits, the event stays pending
        for later delivery, and financials are already correct."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["WK-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                             json={"product_id": pub, "quantity": 1,
                                   "client_order_id": "p13l-wk"})
        self.assertEqual(r.status_code, 201)
        oid = r.get_json()["data"]["order_id"]
        # no worker runs here — the event waits, money is right
        db.cur.execute(
            "SELECT status FROM webhook_outbox WHERE aggregate_id=? AND event_type='order.created'",
            (oid,))
        self.assertEqual(db.cur.fetchone()[0], "pending")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 49000)

    def test_no_redis_in_architecture(self):
        import subprocess
        out = subprocess.run(["grep", "-rli", "redis", "reseller_api/"],
                             capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(out.stdout.strip(), "")

    def test_api_restart_state_survives(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["RST-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        r1 = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                              json={"product_id": pub, "quantity": 1,
                                    "client_order_id": "p13l-rst"})
        self.assertEqual(r1.status_code, 201)
        db.close_thread_connection()   # simulated process restart
        r2 = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                              json={"product_id": pub, "quantity": 1,
                                    "client_order_id": "p13l-rst"})
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.get_json()["data"]["order_id"],
                         r1.get_json()["data"]["order_id"])

    # ---------------------------------------------------------- volume

    def test_high_request_volume_stable(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        errors = 0
        slowest = 0.0
        for _ in range(120):
            t0 = time.perf_counter()
            r = self.client.get("/api/v1/products", headers=_hdr(key, sec))
            ms = (time.perf_counter() - t0) * 1000
            slowest = max(slowest, ms)
            if r.status_code != 200:
                errors += 1
        self.assertEqual(errors, 0)
        self.assertLess(slowest, 1500, f"slowest read {slowest:.0f}ms")

    # ---------------------------------------------------------- owner monitoring

    def _admin_client(self):
        import web_admin.app as wa
        wa.app.config["TESTING"] = True
        wa.DB_PATH = _db_path
        c = wa.app.test_client()
        with c.session_transaction() as sess:
            sess["logged_in"] = True
            sess["username"] = "owner"
        return c

    def test_owner_api_health_monitoring(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        hook, _s = rdb.create_webhook(rs["id"], "https://mon.example/h")

        # workload: 1 success + 1 client error + 1 quota/rate 429 + a failed webhook delivery
        self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                         json={"product_id": pub, "quantity": 1,
                               "client_order_id": "p13l-mon"})
        self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                         json={"product_id": pub, "quantity": 0})
        rdb.set_quota_settings(rs["id"], daily_order_quota=1)
        self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                         json={"product_id": pub, "quantity": 1,
                               "client_order_id": "p13l-mon-2"})
        from reseller_api.services.webhook_delivery import record_delivery_attempt
        record_delivery_attempt(
            outbox_event_id=999999, webhook_id=hook["id"], reseller_id=rs["id"],
            attempt=1, request_id="req-mon-1", requested_at="2026-01-01 00:00:00",
            result={"success": False, "http_status": 500, "error_category": "5xx",
                    "responded_at": "2026-01-01 00:00:01", "elapsed_ms": 5},
        )

        ac = self._admin_client()
        r = ac.get("/api/monitoring/api-health")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        day = body["traffic"]["last_24h"]
        self.assertGreaterEqual(day["total"], 3)
        self.assertGreaterEqual(day["errors_4xx"], 1)
        self.assertGreaterEqual(day["rate_limited_429"], 1)
        self.assertIn("avg_ms", day)
        self.assertGreaterEqual(body["webhooks"]["deliveries_failed_24h"], 1)
        self.assertIn("outbox_pending", body["webhooks"])
        self.assertTrue(body["database"]["ok"])
        # no sensitive material
        import json as _json
        flat = _json.dumps(body).lower()
        for banned in ("secret", "password", "user_agent", "127.0.0.1"):
            self.assertNotIn(banned, flat)

    def test_monitoring_endpoint_is_owner_only(self):
        import web_admin.app as wa
        anon = wa.app.test_client()
        r = anon.get("/api/monitoring/api-health")
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
