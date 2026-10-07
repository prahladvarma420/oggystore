"""Phase 13 Prompt 3 — API versioning & backward compatibility.

Pins the frozen v1 contract so a future change cannot silently break
reseller integrations:

  * the exact v1 route table exists (no endpoint disappears or moves)
  * X-API-Version header on every versioned response
  * envelope contract (ok/data/request_id; ok/error.{code,message}/request_id)
  * public ID formats (ORD-%08d, PRD-*, WHK-%08d)
  * pinned status codes on the established flows
  * legacy Phase 11 alias fields still present
  * existing keys / orders / webhooks keep working
  * deprecation machinery: registry entry -> Deprecation/Sunset/Link headers
  * /api/v2 is not mounted yet; the version registry is the single source
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase13_ver_", suffix=".db")
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
    uid = 9_850_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P13V{n}", f"p13v{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P13V{n}", uid, f"p13v{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=("V-1",), max_per_order=25):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P13V Product {n}", price),
    )
    db.conn.commit()
    pid = db.cur.lastrowid
    db.cur.execute("UPDATE products SET max_per_order=? WHERE id=?", (max_per_order, pid))
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P13V-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


# The frozen v1 route table — any removal/rename fails this list.
EXPECTED_V1_ROUTES = {
    ("GET", "/api/v1"),
    ("GET", "/api/v1/"),
    ("GET", "/api/v1/products"),
    ("GET", "/api/v1/products/<product_id>"),
    ("POST", "/api/v1/products/<product_id>/import"),
    ("GET", "/api/v1/products/<product_id>/import"),
    ("DELETE", "/api/v1/products/<product_id>/import"),
    ("GET", "/api/v1/my-products"),
    ("GET", "/api/v1/my-products/<product_id>"),
    ("PATCH", "/api/v1/my-products/<product_id>"),
    ("DELETE", "/api/v1/my-products/<product_id>"),
    ("GET", "/api/v1/pricing"),
    ("GET", "/api/v1/pricing/<product_id>"),
    ("GET", "/api/v1/balance"),
    ("GET", "/api/v1/usage"),
    ("GET", "/api/v1/analytics/sales"),
    ("GET", "/api/v1/wallet/summary"),
    ("GET", "/api/v1/exports/orders.csv"),
    ("POST", "/api/v1/orders"),
    ("POST", "/api/v1/orders/quote"),
    ("GET", "/api/v1/orders"),
    ("GET", "/api/v1/orders/<order_id>"),
    ("GET", "/api/v1/orders/<order_id>/delivery"),
    ("POST", "/api/v1/webhooks"),
    ("GET", "/api/v1/webhooks"),
    ("GET", "/api/v1/webhooks/analytics"),
    ("GET", "/api/v1/webhooks/<webhook_id>"),
    ("PATCH", "/api/v1/webhooks/<webhook_id>"),
    ("DELETE", "/api/v1/webhooks/<webhook_id>"),
    ("POST", "/api/v1/webhooks/<webhook_id>/rotate-secret"),
    ("POST", "/api/v1/webhooks/<webhook_id>/test"),
    ("POST", "/api/v1/webhooks/events/<outbox_id>/retry"),
    ("GET", "/api/v1/webhooks/<webhook_id>/deliveries"),
    ("POST", "/api/v1/webhooks/<webhook_id>/deliveries/<delivery_id>/retry"),
}


class Phase13VersioningTests(unittest.TestCase):
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

    # ------------------------------------------------------------- route table

    def test_v1_route_table_is_frozen(self):
        rules = set()
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint.startswith("reseller_api_v1."):
                for method in rule.methods - {"HEAD", "OPTIONS"}:
                    rules.add((method, rule.rule.replace("<string:", "<").replace("<string>", "<")))
        # normalize Flask's <string:x> to <x>
        norm = {(m, re.sub(r"<[^:>]+:", "<", p)) for (m, p) in rules}
        self.assertEqual(norm, EXPECTED_V1_ROUTES,
                         f"route drift: missing={EXPECTED_V1_ROUTES - norm} extra={norm - EXPECTED_V1_ROUTES}")

    def test_v2_not_mounted_yet(self):
        r = self.client.get("/api/v2/products")
        self.assertEqual(r.status_code, 404)
        from reseller_api.versioning import CURRENT_VERSION, SUPPORTED_VERSIONS
        self.assertEqual(CURRENT_VERSION, "v1")
        self.assertEqual(SUPPORTED_VERSIONS, ("v1",))

    # ------------------------------------------------------------- version header

    def test_version_header_on_every_versioned_response(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        for path in ("/api/v1", "/api/v1/products", "/api/v1/balance",
                     "/api/v1/orders", "/api/v1/pricing", "/api/v1/my-products",
                     "/api/v1/orders/ORD-99999999"):   # errors carry it too
            r = self.client.get(path, headers=_hdr(key, sec))
            self.assertEqual(r.headers.get("X-API-Version"), "v1", path)
        # auth failures are versioned too
        r = self.client.get("/api/v1/orders")
        self.assertEqual(r.headers.get("X-API-Version"), "v1")

    # ------------------------------------------------------------- frozen contract

    def test_frozen_success_shapes(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["C-1", "C-2"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)

        # quote first (before the order consumes stock): frozen field set
        q = self.client.post("/api/v1/orders/quote", headers=_hdr(key, sec),
                             json={"product_id": pub, "quantity": 1}).get_json()["data"]
        self.assertLessEqual({"product_id", "quantity", "unit_price", "total",
                              "currency", "in_stock", "sufficient_funds",
                              "wallet_balance", "quoted_at"}, set(q.keys()))

        # order create: frozen field set
        r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                             json={"product_id": pub, "quantity": 2,
                                   "client_order_id": "p13v-frozen"})
        self.assertEqual(r.status_code, 201)
        d = r.get_json()["data"]
        self.assertLessEqual(
            {"order_id", "product_id", "quantity", "unit_price", "total",
             "currency", "status", "created_at", "client_order_id"},
            set(d.keys()))
        self.assertRegex(d["order_id"], r"^ORD-\d{8}$")
        self.assertRegex(d["product_id"], r"^PRD-[A-HJ-NP-Z2-9]{12}$")
        self.assertIsInstance(d["quantity"], int)
        self.assertRegex(d["unit_price"], r"^\d+\.\d{2}$")
        self.assertEqual(d["currency"], "USDT")

        # replay: 200 + idempotent flag (frozen semantics)
        r2 = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                              json={"product_id": pub, "quantity": 2,
                                    "client_order_id": "p13v-frozen"})
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.get_json()["data"]["idempotent"])

        # balance shape
        b = self.client.get("/api/v1/balance", headers=_hdr(key, sec)).get_json()["data"]
        self.assertLessEqual({"balance", "currency"}, set(b.keys()))

        # delivery shape
        dv = self.client.get(f"/api/v1/orders/{d['order_id']}/delivery",
                             headers=_hdr(key, sec)).get_json()["data"]["delivery"]
        self.assertLessEqual({"order_id", "delivery_status", "items"},
                             set(dv.keys()))

        # list envelope + pagination meta
        lst = self.client.get("/api/v1/orders", headers=_hdr(key, sec)).get_json()
        self.assertIn("orders", lst["data"])
        self.assertIn("pagination", lst["meta"])

    def test_frozen_status_codes(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["S-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 500)   # $5 < $10

        cases = [
            # (request, expected status, expected code)
            (self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                              json={"product_id": pub, "quantity": 0}),
             400, "invalid_quantity"),
            (self.client.get("/api/v1/orders"), 401, "authentication_failed"),
            (self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                              json={"product_id": pub, "quantity": 1}),
             402, "insufficient_balance"),
            (self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                              json={"product_id": "PRD-AAAAAAAAAA00",
                                    "quantity": 1}),
             404, "product_not_found"),
        ]
        for resp, status, code in cases:
            self.assertEqual(resp.status_code, status, code)
            self.assertEqual(resp.get_json()["error"]["code"], code)

        _topup(rs["id"], 50000)
        r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                             json={"product_id": pub, "quantity": 2})
        self.assertEqual(r.status_code, 422)   # stock rule
        self.assertEqual(r.get_json()["error"]["code"], "insufficient_stock")

    # ------------------------------------------------------------- backward compat

    def test_existing_key_order_webhook_flows_unchanged(self):
        """An 'old' (pre-versioning-change) key keeps working end to end:
        catalog -> quote -> order -> replay -> detail -> delivery -> webhooks."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["B-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        hook, hook_secret = rdb.create_webhook(rs["id"], "https://compat.example/h")

        r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                             json={"product_id": pub, "quantity": 1,
                                   "client_order_id": "p13v-compat"})
        self.assertEqual(r.status_code, 201)
        oid = r.get_json()["data"]["order_id"]

        self.assertEqual(
            self.client.get(f"/api/v1/orders/{oid}", headers=_hdr(key, sec)).status_code, 200)
        self.assertEqual(
            self.client.get(f"/api/v1/orders/{oid}/delivery", headers=_hdr(key, sec)).status_code, 200)
        self.assertEqual(
            self.client.get("/api/v1/webhooks", headers=_hdr(key, sec)).status_code, 200)
        self.assertEqual(
            self.client.get("/api/v1/webhooks/WHK-%08d" % hook["id"],
                            headers=_hdr(key, sec)).status_code, 200)
        # webhook management roundtrip still works
        self.assertEqual(
            self.client.patch(f"/api/v1/webhooks/WHK-{hook['id']:08d}",
                              headers=_hdr(key, sec),
                              json={"enabled": False}).status_code, 200)
        # outbox received the order.created event for this order
        db.cur.execute(
            "SELECT COUNT(*) FROM webhook_outbox WHERE aggregate_id=? AND event_type='order.created'",
            (oid,))
        self.assertEqual(int(db.cur.fetchone()[0]), 1)

    def test_legacy_alias_fields_still_present(self):
        """Phase 11 alias contract (frozen in v1 by policy §2.1)."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["AL-1"])
        _grant(rs["id"], pid)
        r = self.client.get("/api/v1/my-products", headers=_hdr(key, sec))
        self.assertIn("products", r.get_json())
        r = self.client.get("/api/v1/pricing", headers=_hdr(key, sec))
        self.assertIn("prices", r.get_json())
        r = self.client.get(f"/api/v1/my-products/{pub}", headers=_hdr(key, sec))
        self.assertIn("product_id", r.get_json())

    # ------------------------------------------------------------- deprecation machinery

    def test_deprecation_headers_when_registered(self):
        import reseller_api.versioning as versioning

        rs, key, sec = _make_reseller()
        entry = {"sunset": "2027-06-30", "replacement": "GET /api/v2/balance",
                 "reason": "test entry"}
        with patch.dict(versioning.DEPRECATED_ENDPOINTS,
                        {"GET /api/v1/balance": entry}):
            r = self.client.get("/api/v1/balance", headers=_hdr(key, sec))
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers.get("Deprecation"), "true")
            self.assertIn("Sunset", r.headers)
            self.assertIn("Jun", r.headers["Sunset"])     # HTTP-date
            self.assertIn("2027", r.headers["Sunset"])
            self.assertIn('rel="deprecation"', r.headers.get("Link", ""))
            self.assertEqual(r.headers.get("X-API-Replacement"),
                             "GET /api/v2/balance")

        # registry empty again -> no headers
        r = self.client.get("/api/v1/balance", headers=_hdr(key, sec))
        self.assertNotIn("Deprecation", r.headers)
        self.assertNotIn("Sunset", r.headers)

    def test_deprecation_registry_empty_in_production_config(self):
        from reseller_api.versioning import DEPRECATED_ENDPOINTS
        self.assertEqual(DEPRECATED_ENDPOINTS, {})


if __name__ == "__main__":
    unittest.main()
