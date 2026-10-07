"""Phase 13 Prompt 6 — reseller API usage, statistics & monitoring.

Verifies:
  * GET /api/v1/usage — reseller-safe self visibility (envelope, real counts,
    quota remaining math, webhook health, last activity)
  * the shared aggregation rdb.get_reseller_api_usage (single source for the
    reseller endpoint AND the owner WebAdmin detail)
  * owner WebAdmin detail gains the usage block (additive) and stays
    login-gated; resellers have no WebAdmin access
  * privacy: no IPs, user agents, key material, or foreign data anywhere
  * counters are real: a known workload reproduces exactly in the metrics
  * api logs carry request id / reseller / endpoint / method / status /
    latency / timestamp and never the secret
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase13_usage_", suffix=".db")
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
    uid = 9_810_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P13U{n}", f"p13u{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P13U{n}", uid, f"p13u{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=("U-1", "U-2", "U-3")):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P13U Product {n}", price),
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P13U-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


class Phase13UsageMonitoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # NOTE: persist_audit_record is deliberately NOT patched — usage
        # metrics must be computed from real log rows.
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

    # ---------------------------------------------------------- reseller view

    def test_usage_empty_for_new_reseller(self):
        rs, key, sec = _make_reseller()
        r = self.client.get("/api/v1/usage", headers=_hdr(key, sec))
        self.assertEqual(r.status_code, 200)
        u = r.get_json()["data"]["usage"]
        self.assertEqual(u["requests"]["total"], 0)
        self.assertEqual(u["orders"]["total"], 0)
        self.assertIsNone(u["quota"]["daily_limit"])       # unlimited
        self.assertIsNone(u["quota"]["daily_remaining"])
        self.assertEqual(u["webhooks"]["configured"], 0)

    def test_usage_reflects_real_workload(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant(rs["id"], pid)
        _topup(rs["id"], 100000)
        rdb.set_quota_settings(rs["id"], daily_order_quota=10)
        rdb.create_webhook(rs["id"], "https://usage.example/hook")

        # workload: 2 orders + 1 validation failure + a few reads
        for i in range(2):
            r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                                 json={"product_id": pub, "quantity": 1,
                                       "client_order_id": f"p13u-ord-{i}"})
            self.assertEqual(r.status_code, 201)
        bad = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                               json={"product_id": pub, "quantity": 0})
        self.assertEqual(bad.status_code, 400)
        self.client.get("/api/v1/products", headers=_hdr(key, sec))
        self.client.get("/api/v1/balance", headers=_hdr(key, sec))

        u = self.client.get("/api/v1/usage", headers=_hdr(key, sec)).get_json()["data"]["usage"]
        self.assertEqual(u["orders"]["total"], 2)
        self.assertEqual(u["orders"]["today"], 2)
        self.assertEqual(u["orders"]["volume"], "20.00")
        self.assertEqual(u["quota"]["daily_limit"], 10)
        self.assertEqual(u["quota"]["daily_used"], 2)
        self.assertEqual(u["quota"]["daily_remaining"], 8)
        self.assertEqual(u["requests"]["failed"], 1)
        self.assertGreaterEqual(u["requests"]["total"], 5)   # 2 orders + 400 + 2 reads + this
        self.assertEqual(u["requests"]["total"],
                         u["requests"]["successful"] + u["requests"]["failed"])
        self.assertEqual(u["webhooks"]["configured"], 1)
        self.assertEqual(u["webhooks"]["enabled"], 1)
        self.assertIsNotNone(u["activity"]["last_request_at"])
        self.assertIsNotNone(u["activity"]["last_key_used_at"])

    def test_usage_isolation_between_resellers(self):
        rs1, key1, sec1 = _make_reseller()
        rs2, key2, sec2 = _make_reseller()
        pid, pub = _make_product()
        _grant(rs1["id"], pid)
        _topup(rs1["id"], 50000)
        self.client.post("/api/v1/orders", headers=_hdr(key1, sec1),
                         json={"product_id": pub, "quantity": 1,
                               "client_order_id": "p13u-iso-1"})

        u2_first = self.client.get("/api/v1/usage", headers=_hdr(key2, sec2)).get_json()["data"]["usage"]
        self.assertEqual(u2_first["orders"]["total"], 0)
        self.assertEqual(u2_first["requests"]["total"], 0)
        # the audit row for a request is persisted AFTER its response, so the
        # second read sees exactly one prior request — and still no orders
        u2 = self.client.get("/api/v1/usage", headers=_hdr(key2, sec2)).get_json()["data"]["usage"]
        self.assertEqual(u2["orders"]["total"], 0)     # rs1's order never leaks
        self.assertEqual(u2["requests"]["total"], 1)   # only rs2's own first call

        u1 = self.client.get("/api/v1/usage", headers=_hdr(key1, sec1)).get_json()["data"]["usage"]
        self.assertEqual(u1["orders"]["total"], 1)

    def test_usage_privacy_no_internal_fields(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self.client.get("/api/v1/balance", headers=_hdr(key, sec))
        r = self.client.get("/api/v1/usage", headers=_hdr(key, sec))
        import json as _json
        flat = _json.dumps(r.get_json()).lower()
        for banned in ("secret", "user_agent", "password",
                       "reseller_id", "127.0.0.1"):
            self.assertNotIn(banned, flat)
        # the PUBLIC key id is allowed (documented, non-secret); the SECRET
        # value must never appear
        self.assertNotIn(sec.lower(), flat)
        self.assertIn(key, r.get_json()["data"]["usage"]["keys"][0]["key_id"])

    def test_usage_requires_auth(self):
        r = self.client.get("/api/v1/usage")
        self.assertEqual(r.status_code, 401)

    # ---------------------------------------------------------- owner webadmin

    def _admin_client(self):
        import web_admin.app as wa
        wa.app.config["TESTING"] = True
        wa.DB_PATH = _db_path
        c = wa.app.test_client()
        with c.session_transaction() as sess:
            sess["logged_in"] = True
            sess["username"] = "owner"
        return c

    def test_webadmin_detail_includes_usage_block(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                         json={"product_id": pub, "quantity": 1,
                               "client_order_id": "p13u-wa-1"})
        self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                         json={"product_id": pub, "quantity": 0})

        ac = self._admin_client()
        r = ac.get(f"/api/resellers/{rs['public_id']}")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertIn("usage", body)                    # additive block
        u = body["usage"]
        self.assertEqual(u["orders_total"], 1)
        self.assertEqual(u["requests_failed"], 1)
        self.assertEqual(u["order_volume"], "10.00")
        self.assertIn("rate_limit_events", u)
        self.assertIn("webhook_deliveries_failed", u)
        self.assertIn("quota_daily_remaining", u)
        # legacy fields preserved (backward compat)
        self.assertIn("api_stats", body)
        self.assertIn("orders_count", body)

    def test_webadmin_still_owner_only(self):
        rs, key, sec = _make_reseller()
        ac = self._admin_client()
        # logged out client gets 401 on api routes
        import web_admin.app as wa
        anon = wa.app.test_client()
        r = anon.get(f"/api/resellers/{rs['public_id']}")
        self.assertEqual(r.status_code, 401)
        # a reseller API key is meaningless here
        r2 = anon.get(f"/api/resellers/{rs['public_id']}",
                      headers=_hdr(key, sec))
        self.assertEqual(r2.status_code, 401)

    # ---------------------------------------------------------- logging fields

    def test_api_logs_carry_operational_fields_no_secrets(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                             json={"product_id": pub, "quantity": 1,
                                   "client_order_id": "p13u-log-1"})
        self.assertEqual(r.status_code, 201)
        rid_used = r.get_json()["request_id"]

        db.cur.execute(
            "SELECT reseller_id, api_key_id, endpoint, method, status_code,"
            " request_id, response_ms, created, key_id"
            " FROM reseller_api_logs WHERE request_id=?", (rid_used,))
        row = db.cur.fetchone()
        self.assertIsNotNone(row)
        (r_id, key_row_id, endpoint, method, status_code,
         request_id, response_ms, created, key_public) = row
        self.assertEqual(r_id, rs["id"])
        self.assertEqual(key_row_id, rdb.get_api_key(key)["id"])
        self.assertEqual(endpoint, "/api/v1/orders")
        self.assertEqual(method, "POST")
        self.assertEqual(status_code, 201)
        self.assertEqual(request_id, rid_used)
        self.assertIsNotNone(response_ms)
        self.assertTrue(created)
        self.assertEqual(key_public, key)         # public key id only
        # the secret is nowhere in the logs table
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_api_logs"
            " WHERE user_agent LIKE ? OR endpoint LIKE ? OR request_id LIKE ?",
            (f"%{sec}%",) * 3)
        self.assertEqual(int(db.cur.fetchone()[0]), 0)


if __name__ == "__main__":
    unittest.main()
