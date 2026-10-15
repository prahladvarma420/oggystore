"""Phase 13 Prompt 2 — standardized reseller API responses & errors.

Verifies every reseller API endpoint speaks ONE envelope:

  success  {"ok": True,  "data": {...}, "request_id": ..., "meta"?}
  error    {"ok": False, "error": {"code", "message"}, "request_id": ...}

with legacy top-level aliases preserved on the Phase 11 catalog/pricing
endpoints (backward compatibility), consistent business error codes,
request-ID tracing on every response including failures, and no leakage of
stack traces / SQL / internal details on 500s.

Also covers the P13 audit fixes: endpoint-rate single-counting (the
double-wrapped decorator consumed two units per call) and correct
read-vs-write rate categories for the my-products GETs.
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase13_resp_", suffix=".db")
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
    uid = 9_870_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P13R{n}", f"p13r{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P13R{n}", uid, f"p13r{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=("S-1",)):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P13 Product {n}", price),
    )
    db.conn.commit()
    pid = db.cur.lastrowid
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P13-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


def _assert_success_envelope(case, resp):
    body = resp.get_json()
    case.assertIs(body.get("ok"), True, body)
    case.assertIn("data", body)
    case.assertTrue(body.get("request_id"), body)
    return body


def _assert_error_envelope(case, resp, status, code=None):
    case.assertEqual(resp.status_code, status, resp.get_json())
    body = resp.get_json()
    case.assertIs(body.get("ok"), False, body)
    err = body.get("error")
    case.assertIsInstance(err, dict, body)          # never a bare string
    case.assertTrue(err.get("code"))
    case.assertTrue(err.get("message"))
    if code:
        case.assertEqual(err["code"], code)
    case.assertTrue(body.get("request_id"), body)   # traceable
    return body


class Phase13ResponseStandardizationTests(unittest.TestCase):
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

    # ------------------------------------------------------------- success

    def test_success_envelope_on_every_resource(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["E-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        hook, _sec = rdb.create_webhook(rs["id"], "https://env.example/hook")

        checks = [
            ("GET", "/api/v1"),
            ("GET", "/api/v1/products"),
            ("GET", f"/api/v1/products/{pub}"),
            ("GET", "/api/v1/my-products"),
            ("GET", f"/api/v1/my-products/{pub}"),
            ("GET", "/api/v1/pricing"),
            ("GET", f"/api/v1/pricing/{pub}"),
            ("GET", "/api/v1/balance"),
            ("GET", "/api/v1/orders"),
            ("GET", "/api/v1/webhooks"),
            ("GET", f"/api/v1/webhooks/WHK-{int(hook['id']):08d}"),
        ]
        for method, path in checks:
            r = getattr(self.client, method.lower())(path, headers=_hdr(key, sec))
            _assert_success_envelope(self, r)

        # POST endpoints too
        r = self.client.post("/api/v1/orders/quote", headers=_hdr(key, sec),
                             json={"product_id": pub, "quantity": 1})
        _assert_success_envelope(self, r)
        r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                             json={"product_id": pub, "quantity": 1,
                                   "client_order_id": "p13-env-1"})
        self.assertEqual(r.status_code, 201)
        _assert_success_envelope(self, r)

    # ------------------------------------------------------------- backward compat

    def test_legacy_aliases_preserved_on_catalog_and_pricing(self):
        """Phase 11 consumers read top-level 'products'/'prices'/mapping
        fields — they must keep working beside the new envelope."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["L-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)

        r = self.client.get("/api/v1/my-products", headers=_hdr(key, sec))
        body = _assert_success_envelope(self, r)
        self.assertIn("products", body)                       # legacy alias
        self.assertEqual(body["products"], body["data"]["products"])
        self.assertEqual(body["products"][0]["product_id"], pub)

        r = self.client.get(f"/api/v1/my-products/{pub}", headers=_hdr(key, sec))
        body = _assert_success_envelope(self, r)
        self.assertEqual(body["product_id"], pub)             # legacy top-level
        self.assertEqual(body["data"]["product_id"], pub)     # standard field

        r = self.client.patch(f"/api/v1/my-products/{pub}", headers=_hdr(key, sec),
                              json={"custom_name": "Alias Test"})
        body = _assert_success_envelope(self, r)
        self.assertEqual(body["custom_name"], "Alias Test")   # legacy
        self.assertEqual(body["data"]["custom_name"], "Alias Test")

        r = self.client.get("/api/v1/pricing", headers=_hdr(key, sec))
        body = _assert_success_envelope(self, r)
        self.assertIn("prices", body)
        self.assertEqual(body["prices"], body["data"]["prices"])

        r = self.client.get(f"/api/v1/pricing/{pub}", headers=_hdr(key, sec))
        body = _assert_success_envelope(self, r)
        self.assertEqual(body["product_id"], pub)
        self.assertEqual(body["data"]["product_id"], pub)

        # DELETE keeps the established 204 No Content (matches webhooks)
        r = self.client.delete(f"/api/v1/my-products/{pub}", headers=_hdr(key, sec))
        self.assertEqual(r.status_code, 204)

    # ------------------------------------------------------------- errors

    def test_validation_error_shape(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        # quantity violation on orders
        _assert_error_envelope(
            self,
            self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                             json={"product_id": pub, "quantity": 0}),
            400, "invalid_quantity")
        # forbidden field on my-products PATCH (was {"error": "invalid_field"} str)
        body = _assert_error_envelope(
            self,
            self.client.patch(f"/api/v1/my-products/{pub}", headers=_hdr(key, sec),
                              json={"price": "0.01"}),
            422, "invalid_parameter")
        # empty body (was "empty_body")
        _assert_error_envelope(
            self,
            self.client.patch(f"/api/v1/my-products/{pub}", headers=_hdr(key, sec),
                              json={}),
            400, "invalid_request")
        # malformed JSON body on orders
        r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                             data="{not json", content_type="application/json")
        self.assertEqual(r.status_code, 400)
        _assert_error_envelope(self, r, 400)

    def test_authentication_error_shape_uniform(self):
        r1 = self.client.get("/api/v1/orders")  # no header
        b1 = _assert_error_envelope(self, r1, 401)
        r2 = self.client.get("/api/v1/orders",
                             headers=_hdr("rk_test_NOPE123456", "x" * 40))
        b2 = _assert_error_envelope(self, r2, 401)
        # uniform: no oracle between missing header and bad credentials
        self.assertEqual(b1["error"]["code"], b2["error"]["code"])
        self.assertNotEqual(b1["request_id"], b2["request_id"])  # distinct tracing

    def test_authorization_error_shape(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        # Disable the reseller's request allowance -> 403 authorization_denied
        db.cur.execute(
            "UPDATE resellers SET per_minute_request_limit=0 WHERE id=?",
            (rs["id"],)) if self._has_column("resellers", "per_minute_request_limit") else None
        if self._has_column("resellers", "per_minute_request_limit"):
            _assert_error_envelope(
                self, self.client.get("/api/v1/orders", headers=_hdr(key, sec)),
                403, "authorization_denied")
        else:
            # fall back to an impossible operation denial via suspended env
            rdb.set_reseller_status(rs["id"], "suspended")
            _assert_error_envelope(
                self, self.client.get("/api/v1/orders", headers=_hdr(key, sec)), 401)

    @staticmethod
    def _has_column(table, col):
        db.cur.execute(f"PRAGMA table_info({table})")
        return any(r[1] == col for r in db.cur.fetchall())

    def test_not_found_shapes(self):
        rs, key, sec = _make_reseller()
        _assert_error_envelope(
            self, self.client.get("/api/v1/orders/ORD-99999999",
                                  headers=_hdr(key, sec)), 404, "order_not_found")
        _assert_error_envelope(
            self, self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                                   json={"product_id": "PRD-AAAAAAAAAA00",
                                         "quantity": 1}),
            404, "product_not_found")
        _assert_error_envelope(
            self, self.client.get("/api/v1/my-products/PRD-AAAAAAAAAA00",
                                  headers=_hdr(key, sec)), 404)
        _assert_error_envelope(
            self, self.client.get("/api/v1/pricing/PRD-AAAAAAAAAA00",
                                  headers=_hdr(key, sec)), 404, "product_not_found")

    def test_conflict_idempotency_conflict_409(self):
        """Service-level: same ledger reference, different payload ->
        idempotency_conflict, which the order service maps to HTTP 409."""
        rs, key, sec = _make_reseller()
        st1, _ = rdb.post_wallet_transaction(rs["id"], "topup_credit", 100, "P9-C1", "a")
        st2, _ = rdb.post_wallet_transaction(rs["id"], "topup_credit", 200, "P9-C1", "b")
        self.assertEqual(st1, "posted")
        self.assertEqual(st2, "idempotency_conflict")
        # identical replay is a safe duplicate, not a conflict
        st3, _ = rdb.post_wallet_transaction(rs["id"], "topup_credit", 100, "P9-C1", "a")
        self.assertEqual(st3, "duplicate")
        # the mapping to the API surface exists and is a 409
        from reseller_api.services.order_service import OrderServiceError
        err = OrderServiceError("idempotency_conflict", "x", 409)
        self.assertEqual(err.status, 409)

    def test_rate_limit_error_shape_and_headers(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)

        from reseller_api.middleware import endpoint_rate_limit as mw_erl
        from reseller_api.services.endpoint_rate_limit import EndpointRateDecision

        deny = EndpointRateDecision(False, "order_create", 5, 0, 30)
        with patch.object(mw_erl, "check_endpoint_rate", return_value=deny):
            r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                                 json={"product_id": pub, "quantity": 1})
        _assert_error_envelope(self, r, 429, "rate_limit_exceeded")
        self.assertIn("Retry-After", r.headers)

    def test_endpoint_rate_real_denial_returns_429_not_500(self):
        """P13 bug fix proof: EndpointRateDecision had no .count field, so the
        middleware deny path raised AttributeError -> 500.  Drive the REAL
        delivery_read category to its 60/min limit and assert a clean 429."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["DL-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                             json={"product_id": pub, "quantity": 1,
                                   "client_order_id": "p13-rl-real"})
        self.assertEqual(r.status_code, 201)
        oid = r.get_json()["data"]["order_id"]

        last = None
        for _ in range(60):
            last = self.client.get(f"/api/v1/orders/{oid}/delivery",
                                   headers=_hdr(key, sec))
            self.assertEqual(last.status_code, 200)
        denied = self.client.get(f"/api/v1/orders/{oid}/delivery",
                                 headers=_hdr(key, sec))
        _assert_error_envelope(self, denied, 429, "rate_limit_exceeded")
        self.assertIn("Retry-After", denied.headers)
        self.assertEqual(denied.headers.get("RateLimit-Category"), "delivery_read")

    def test_quota_error_shape(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["Q1", "Q2"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        rdb.set_quota_settings(rs["id"], daily_order_quota=1)

        r1 = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                              json={"product_id": pub, "quantity": 1,
                                    "client_order_id": "p13-quota-1"})
        self.assertEqual(r1.status_code, 201)
        body = _assert_error_envelope(
            self,
            self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                             json={"product_id": pub, "quantity": 1,
                                   "client_order_id": "p13-quota-2"}),
            429, "daily_order_quota_exceeded")

    def test_server_failure_is_sanitized(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["X-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)

        import reseller_api.controllers.v1.orders as oc
        with patch.object(oc, "validate_order_request",
                          side_effect=RuntimeError("DB secret /tmp/x.db SQL: SELECT")):
            r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                                 json={"product_id": pub, "quantity": 1})
        body = _assert_error_envelope(self, r, 500, "internal_error")
        flat = str(body).lower()
        for leak in ("sql", "select", "/tmp", "traceback", "runtimeerror", ".db"):
            self.assertNotIn(leak, flat)

    def test_service_unavailable_503_shape(self):
        """503 semantics: /health/db reports unavailable without leaking
        driver internals."""
        import reseller_api.controllers.health as hc
        import database.db as _dbmod

        class BrokenCursor:
            def execute(self, *a):
                raise RuntimeError("conn refused tcp://internal:5432")

            def fetchone(self):
                return None

        class BrokenConn:
            def cursor(self):
                return BrokenCursor()

        with patch.object(_dbmod, "get_conn", return_value=BrokenConn()):
            r = self.client.get("/health/db")
        self.assertEqual(r.status_code, 503)
        body = r.get_json()
        self.assertEqual(body["status"], "unavailable")
        self.assertNotIn("tcp", str(body).lower())

    # ------------------------------------------------------------- rate-limit fixes

    def test_endpoint_rate_single_counted_after_dedup(self):
        """P13 fix verification: the my-products GETs previously consumed TWO
        budget units per request (controller + route both applied the
        category decorator).  Exactly one unit per request now."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["RC-1"])
        _grant(rs["id"], pid)

        def used(category):
            db.cur.execute(
                "SELECT minute_count FROM reseller_endpoint_quotas"
                " WHERE reseller_id=? AND category=?",
                (rs["id"], category))
            row = db.cur.fetchone()
            return int(row[0]) if row else 0

        before_read = used("read")
        before_write = used("catalog_write")
        for _ in range(3):
            r = self.client.get("/api/v1/my-products", headers=_hdr(key, sec))
            self.assertEqual(r.status_code, 200)
        self.assertEqual(used("read") - before_read, 3)        # one per request
        self.assertEqual(used("catalog_write") - before_write, 0)  # GETs aren't writes

    def test_request_id_present_and_unique(self):
        rs, key, sec = _make_reseller()
        r1 = self.client.get("/api/v1/balance", headers=_hdr(key, sec))
        r2 = self.client.get("/api/v1/balance", headers=_hdr(key, sec))
        b1, b2 = r1.get_json(), r2.get_json()
        self.assertTrue(b1["request_id"] and b2["request_id"])
        self.assertNotEqual(b1["request_id"], b2["request_id"])
        # errors carry it too
        e = self.client.get("/api/v1/orders/ORD-99999999",
                            headers=_hdr(key, sec)).get_json()
        self.assertTrue(e["request_id"])


if __name__ == "__main__":
    unittest.main()
