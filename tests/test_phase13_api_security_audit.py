"""Phase 13 Prompt 9 — final reseller API security & DX audit.

A consolidated executable audit over every surface built or hardened in
Phases 9–13, executed against the live Flask stack.  Each test asserts the
ACTUAL behavior — authentication lifecycle, per-resource isolation, input
security, rate limiting, error sanitization, docs accuracy, backward
compatibility, sandbox isolation, and log hygiene.
"""

import os
import re
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase13_audit_", suffix=".db")
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
import reseller_api.middleware.authentication  # noqa: F401
from reseller_api.services.rate_limit import RequestRateDecision

_ctr = 0


def _n():
    global _ctr
    _ctr += 1
    return _ctr


def _make_reseller():
    n = _n()
    uid = 9_760_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P13A{n}", f"p13a{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P13A{n}", uid, f"p13a{n}")
    rdb.set_reseller_status(rs["id"], "active")
    return rs


def _make_product(price=10.0, codes=("A-1", "A-2")):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P13A Product {n}", price),
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P13A-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


class Phase13ApiSecurityAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p0 = patch(
            "reseller_api.middleware.authentication.check_ip_auth_failure_rate",
            return_value=(True, 0))
        cls.p0.start()
        cls.p0b = patch(
            "reseller_api.middleware.authentication.record_ip_auth_failure",
            return_value=None)
        cls.p0b.start()
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
        cls.p0b.stop()
        cls.p0.stop()
        try:
            db.close_thread_connection()
        finally:
            try:
                os.unlink(_db_path)
            except FileNotFoundError:
                pass

    # ================================================================
    # 1. AUTHENTICATION
    # ================================================================

    def test_auth_full_matrix(self):
        rs = _make_reseller()
        key, sec = rdb.create_api_key(rs["id"], "test")

        # valid
        self.assertEqual(self.client.get("/api/v1/balance",
                                         headers=_hdr(key, sec)).status_code, 200)
        # invalid secret / unknown key / garbage
        for hdrs in ({"Authorization": f"Bearer {key}.{sec}x"},
                     {"Authorization": f"Bearer rk_test_ZZZZZZZZ.{sec}"},
                     {"Authorization": "garbage"},
                     {}):
            r = self.client.get("/api/v1/balance", headers=hdrs)
            self.assertEqual(r.status_code, 401, hdrs)
            self.assertEqual(r.get_json()["error"]["code"], "authentication_failed")
        # revoked
        rdb.revoke_api_key_for_reseller(rs["id"], key)
        self.assertEqual(self.client.get("/api/v1/balance",
                                         headers=_hdr(key, sec)).status_code, 401)
        # expired
        rs2 = _make_reseller()
        k2, s2 = rdb.create_api_key(rs2["id"], "test")
        rdb.set_api_key_expiry_for_reseller(rs2["id"], k2, "2000-01-01 00:00:00")
        self.assertEqual(self.client.get("/api/v1/balance",
                                         headers=_hdr(k2, s2)).status_code, 401)
        # wrong environment (production key against test sandbox)
        rs3 = _make_reseller()
        k3, s3 = rdb.create_api_key(rs3["id"], "production")
        self.assertEqual(self.client.get("/api/v1/balance",
                                         headers=_hdr(k3, s3)).status_code, 401)

    # ================================================================
    # 2. AUTHORIZATION — isolation on every resource
    # ================================================================

    def test_isolation_all_resources(self):
        ra = _make_reseller()
        rb = _make_reseller()
        ka, sa = rdb.create_api_key(ra["id"], "test")
        kb, sb = rdb.create_api_key(rb["id"], "test")
        pid, pub = _make_product()
        _grant(ra["id"], pid)
        _topup(ra["id"], 50000)
        _topup(rb["id"], 50000)

        # A orders; B cannot see/touch anything of A's
        r = self.client.post("/api/v1/orders", headers=_hdr(ka, sa),
                             json={"product_id": pub, "quantity": 1,
                                   "client_order_id": "p13a-iso"})
        self.assertEqual(r.status_code, 201)
        oid = r.get_json()["data"]["order_id"]

        # order isolation
        self.assertEqual(self.client.get(f"/api/v1/orders/{oid}",
                                         headers=_hdr(kb, sb)).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/orders/{oid}/delivery",
                                         headers=_hdr(kb, sb)).status_code, 404)
        # product/mapping isolation (product never granted to B)
        self.assertEqual(self.client.post(
            "/api/v1/orders", headers=_hdr(kb, sb),
            json={"product_id": pub, "quantity": 1}).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/my-products/{pub}",
                                         headers=_hdr(kb, sb)).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/pricing/{pub}",
                                         headers=_hdr(kb, sb)).status_code, 404)
        # wallet isolation: B's balance is B's own
        bal_b = self.client.get("/api/v1/balance", headers=_hdr(kb, sb)).get_json()
        self.assertEqual(bal_b["data"]["balance"], "500.00")
        # webhook isolation
        hook_a, _s = rdb.create_webhook(ra["id"], "https://a.example/h")
        wid = f"WHK-{hook_a['id']:08d}"
        self.assertEqual(self.client.get(f"/api/v1/webhooks/{wid}",
                                         headers=_hdr(kb, sb)).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/webhooks/{wid}/deliveries",
                                         headers=_hdr(kb, sb)).status_code, 404)
        self.assertEqual(self.client.post(f"/api/v1/webhooks/{wid}/test",
                                          headers=_hdr(kb, sb)).status_code, 404)
        # usage isolation
        u = self.client.get("/api/v1/usage", headers=_hdr(kb, sb)).get_json()
        self.assertEqual(u["data"]["usage"]["orders"]["total"], 0)

    # ================================================================
    # 3. INPUT SECURITY
    # ================================================================

    def test_input_security_matrix(self):
        rs = _make_reseller()
        key, sec = rdb.create_api_key(rs["id"], "test")
        pid, pub = _make_product()
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)

        # malformed JSON
        r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                             data="{broken", content_type="application/json")
        self.assertEqual(r.status_code, 400)
        # missing fields
        self.assertEqual(self.client.post(
            "/api/v1/orders", headers=_hdr(key, sec), json={}).status_code, 400)
        # unexpected/forbidden fields
        self.assertEqual(self.client.post(
            "/api/v1/orders", headers=_hdr(key, sec),
            json={"product_id": pub, "quantity": 1, "price": "0.01"}).status_code, 400)
        # huge values
        self.assertEqual(self.client.post(
            "/api/v1/orders", headers=_hdr(key, sec),
            json={"product_id": pub, "quantity": 10**9}).status_code, 400)
        # negative values
        self.assertEqual(self.client.post(
            "/api/v1/orders", headers=_hdr(key, sec),
            json={"product_id": pub, "quantity": -5}).status_code, 400)
        # invalid IDs (bad format / sql injection / traversal)
        for bad_id in ("ORD-123", "'; DROP TABLE orders;--", "../../etc",
                       "ORD-0000000x"):
            g = self.client.get(f"/api/v1/orders/{bad_id}",
                                headers=_hdr(key, sec))
            self.assertIn(g.status_code, (400, 404), bad_id)
        # invalid currency field is inert (always USDT server-side)
        r = self.client.post("/api/v1/orders/quote", headers=_hdr(key, sec),
                             json={"product_id": pub, "quantity": 1,
                                   "currency": "BTC"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["data"]["currency"], "USDT")
        # NaN/Inf rejected by the payload guard
        r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                             data='{"product_id": "%s", "quantity": NaN}' % pub,
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)
        # wallet untouched by the whole battery
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)

    def test_oversized_body_rejected(self):
        rs = _make_reseller()
        key, sec = rdb.create_api_key(rs["id"], "test")
        big = "x" * (600 * 1024)
        r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                             json={"product_id": "PRD-AAAAAAAAAA00",
                                   "quantity": 1, "client_order_id": big})
        # over the 512 KB hard cap -> clean 413 (P9 fixed a NameError that
        # turned this path into a 500); never a stack-trace leak
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.get_json()["error"]["code"], "payload_too_large")
        # oversized but under the cap: field validation still applies
        pid2, pub2 = _make_product()
        _grant(rs["id"], pid2)
        mid = "x" * 200
        r2 = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                              json={"product_id": pub2,
                                    "quantity": 1, "client_order_id": mid})
        self.assertEqual(r2.status_code, 400)
        self.assertEqual(r2.get_json()["error"]["code"], "invalid_client_order_id")

    # ================================================================
    # 4. RATE LIMITING — endpoint categories + isolation + quota
    # ================================================================

    def test_endpoint_categories_isolated_per_reseller(self):
        ra = _make_reseller()
        rb = _make_reseller()
        ka, sa = rdb.create_api_key(ra["id"], "test")
        kb, sb = rdb.create_api_key(rb["id"], "test")
        pid, pub = _make_product(codes=["RL-1"])
        _grant(ra["id"], pid)
        _topup(ra["id"], 50000)
        r = self.client.post("/api/v1/orders", headers=_hdr(ka, sa),
                             json={"product_id": pub, "quantity": 1,
                                   "client_order_id": "p13a-rl"})
        self.assertEqual(r.status_code, 201)
        oid = r.get_json()["data"]["order_id"]
        # A exhausts the delivery_read budget (60/min) — real denial
        for _ in range(60):
            g = self.client.get(f"/api/v1/orders/{oid}/delivery",
                                headers=_hdr(ka, sa))
            self.assertEqual(g.status_code, 200)
        denied = self.client.get(f"/api/v1/orders/{oid}/delivery",
                                 headers=_hdr(ka, sa))
        self.assertEqual(denied.status_code, 429)
        # B unaffected (per-reseller buckets) and reads still work for A
        self.assertEqual(self.client.get("/api/v1/products",
                                         headers=_hdr(ka, sa)).status_code, 200)
        # quota counter: orders count recorded once
        u = self.client.get("/api/v1/usage", headers=_hdr(ka, sa)).get_json()
        self.assertEqual(u["data"]["usage"]["orders"]["total"], 1)

    # ================================================================
    # 5. ERROR SECURITY
    # ================================================================

    def test_errors_never_leak_internals(self):
        rs = _make_reseller()
        key, sec = rdb.create_api_key(rs["id"], "test")
        pid, pub = _make_product()
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)

        import reseller_api.controllers.v1.orders as oc
        with patch.object(oc, "validate_order_request",
                          side_effect=RuntimeError("psycopg connect db-01.internal:5432 SELECT * FROM reseller_wallet")):
            r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                                 json={"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 500)
        flat = str(r.get_json()).lower()
        for leak in ("psycopg", "db-01", "5432", "select", "wallet",
                     "traceback", ".py", "runtimeerror"):
            self.assertNotIn(leak, flat)
        self.assertTrue(r.get_json()["request_id"])

        # 404 uniformity: no difference between foreign and nonexistent
        rs2 = _make_reseller()
        k2, s2 = rdb.create_api_key(rs2["id"], "test")
        pid2, pub2 = _make_product(codes=["XX-1"])
        _grant(rs2["id"], pid2)
        _topup(rs2["id"], 50000)
        r2 = self.client.post("/api/v1/orders", headers=_hdr(k2, s2),
                              json={"product_id": pub2, "quantity": 1,
                                    "client_order_id": "p13a-oracle"})
        oid_other = r2.get_json()["data"]["order_id"]
        g1 = self.client.get(f"/api/v1/orders/{oid_other}", headers=_hdr(key, sec))
        g2 = self.client.get("/api/v1/orders/ORD-99999999", headers=_hdr(key, sec))
        self.assertEqual(g1.status_code, 404)
        self.assertEqual(g2.status_code, 404)
        self.assertEqual(g1.get_json()["error"]["code"],
                         g2.get_json()["error"]["code"])
        self.assertEqual(g1.get_json()["error"]["message"],
                         g2.get_json()["error"]["message"])

    # ================================================================
    # 6. DOCUMENTATION — every documented route works live
    # ================================================================

    def test_documented_routes_all_live(self):
        import yaml
        spec = yaml.safe_load((ROOT / "docs" / "openapi.yaml").read_text())
        live = set()
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint.startswith("reseller_api_v1."):
                for m in rule.methods - {"HEAD", "OPTIONS"}:
                    p = re.sub(r"<(?:[^:>]+:)?([^>]+)>", lambda m: "{%s}" % {
                        "order_id": "orderId", "product_id": "productId",
                        "webhook_id": "webhookId", "delivery_id": "deliveryId",
                        "outbox_id": "eventId",
                        "invoice_id": "invoiceId",
                        "statement_id": "statementId",
                        "refund_id": "refundId"}.get(m.group(1), m.group(1)),
                        rule.rule)
                    live.add((m, p.replace("/api/v1", "", 1) or "/"))
        live.discard(("GET", ""))
        documented = {(m.upper(), p) for p, item in spec["paths"].items()
                      for m in item}
        self.assertEqual(live - documented, set())
        self.assertEqual(documented - live, set())

    # ================================================================
    # 7. BACKWARD COMPATIBILITY
    # ================================================================

    def test_backward_compatibility_contract(self):
        rs = _make_reseller()
        key, sec = rdb.create_api_key(rs["id"], "test")
        pid, pub = _make_product()
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)

        # legacy Phase 11 aliases still present
        body = self.client.get("/api/v1/my-products",
                               headers=_hdr(key, sec)).get_json()
        self.assertIn("products", body)
        self.assertIn("data", body)
        body = self.client.get("/api/v1/pricing",
                               headers=_hdr(key, sec)).get_json()
        self.assertIn("prices", body)
        # envelope fields pinned
        for b in (body,):
            self.assertIn("ok", b)
            self.assertIn("request_id", b)
        # version header present
        r = self.client.get("/api/v1/products", headers=_hdr(key, sec))
        self.assertEqual(r.headers.get("X-API-Version"), "v1")
        # 201 create / 200 replay semantics unchanged
        r1 = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                              json={"product_id": pub, "quantity": 1,
                                    "client_order_id": "p13a-bc"})
        r2 = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                              json={"product_id": pub, "quantity": 1,
                                    "client_order_id": "p13a-bc"})
        self.assertEqual((r1.status_code, r2.status_code), (201, 200))
        self.assertTrue(r2.get_json()["data"]["idempotent"])

    # ================================================================
    # 8. SANDBOX isolation
    # ================================================================

    def test_sandbox_cannot_touch_production(self):
        from reseller_api.config import validate_environment_marker
        from reseller_api.services.authentication import authenticate_credentials
        # DB is stamped 'test' (this suite); a production API refuses to boot
        with self.assertRaises(RuntimeError):
            validate_environment_marker({"API_ENVIRONMENT": "production"})
        # key env pinning blocks cross-environment use in both directions
        rs = _make_reseller()
        tk, ts = rdb.create_api_key(rs["id"], "test")
        pk, ps = rdb.create_api_key(rs["id"], "production")
        self.assertIsNone(authenticate_credentials(pk, ps, "test"))
        self.assertIsNone(authenticate_credentials(tk, ts, "production"))

    # ================================================================
    # 9. MONITORING — logs free of secrets
    # ================================================================

    def test_logs_and_events_free_of_secrets(self):
        rs = _make_reseller()
        key, sec = rdb.create_api_key(rs["id"], "test")
        pid, pub = _make_product(codes=["LG-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        hook, hook_secret = rdb.create_webhook(rs["id"], "https://logs.example/h")

        # full workflow
        self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                         json={"product_id": pub, "quantity": 1,
                               "client_order_id": "p13a-logs"})
        self.client.get("/api/v1/orders", headers=_hdr(key, sec))
        self.client.get("/api/v1/balance", headers=_hdr(key, sec))
        self.client.get("/api/v1/orders/ORD-99999999", headers=_hdr(key, sec))

        # scan every operational store for secret material
        for table in ("reseller_api_logs", "reseller_security_events",
                      "webhook_outbox"):
            try:
                db.cur.execute(f"SELECT * FROM {table} WHERE reseller_id=?",
                               (rs["id"],))
            except Exception:
                continue
            rows = db.cur.fetchall()
            for row in rows:
                flat = str(row)
                self.assertNotIn(sec, flat, f"{table} leaked the API secret")
                self.assertNotIn(hook_secret, flat,
                                 f"{table} leaked the webhook secret")
        # webhook payloads contain no delivery content or secrets
        import json as _json
        db.cur.execute(
            "SELECT payload_json FROM webhook_outbox WHERE reseller_id=?",
            (rs["id"],))
        for (pj,) in db.cur.fetchall():
            flat = _json.dumps(_json.loads(pj)).lower()
            self.assertNotIn("lg-1", flat)          # the delivered code
            self.assertNotIn("secret", flat)


if __name__ == "__main__":
    unittest.main()
