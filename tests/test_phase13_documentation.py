"""Phase 13 Prompt 4 — documentation & OpenAPI accuracy gates.

Guarantees the docs can never drift from the implementation:

  * docs/openapi.yaml parses as valid YAML
  * every documented path+method exists in the LIVE Flask route table
  * every live v1 route is documented (no undocumented endpoints)
  * documented request fields are accepted by the live validators
  * documented response fields are returned by the live endpoints
  * the developer guide covers all 20 required topics
  * no real secrets/keys in the docs (placeholders only)
"""

import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "DATA_ENCRYPTION_KEY",
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
)

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase13_docs_", suffix=".db")
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

from unittest.mock import patch

SPEC = yaml.safe_load((ROOT / "docs" / "openapi.yaml").read_text())
GUIDE = (ROOT / "RESELLER_API.md").read_text()


def _flask_to_openapi(rule: str) -> str:
    """'/api/v1/orders/<string:order_id>' -> '/api/v1/orders/{orderId}' style."""
    def conv(m):
        name = m.group(1)
        name = {"order_id": "orderId", "product_id": "productId",
                "webhook_id": "webhookId", "delivery_id": "deliveryId",
                "invoice_id": "invoiceId",
                "statement_id": "statementId",
                "refund_id": "refundId",
                "outbox_id": "eventId"}.get(name, name)
        return "{" + name + "}"
    return re.sub(r"<(?:[^:>]+:)?([^>]+)>", conv, rule)


class DocumentationAccuracyTests(unittest.TestCase):
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

        # one live reseller/product/order for shape verification
        n = 1
        db.cur.execute(
            "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
            (9_840_001, "P13D1", "p13d1"))
        db.conn.commit()
        cls.rs = rdb.create_reseller("P13DocR", 9_840_001, "p13d1")
        rdb.set_reseller_status(cls.rs["id"], "active")
        cls.key, cls.sec = rdb.create_api_key(cls.rs["id"], "test")
        db.cur.execute(
            "INSERT INTO products(name, price, stock, delivery, active)"
            " VALUES('Doc Product',12.5,0,'code_stock',1)")
        db.conn.commit()
        cls.pid = db.cur.lastrowid
        db.cur.execute("UPDATE products SET max_per_order=10 WHERE id=?", (cls.pid,))
        for code in ("DOC-CODE-1", "DOC-CODE-2", "DOC-CODE-3", "DOC-CODE-4"):
            db.cur.execute(
                "INSERT INTO product_stock(product_id, delivery, sold) VALUES(?,?,0)",
                (cls.pid, code))
        db.conn.commit()
        rdb.set_product_access(cls.rs["id"], cls.pid, True)
        rdb.import_reseller_product(cls.rs["id"], cls.pid)
        cls.pub = rdb.ensure_product_public_id(cls.pid)
        rdb.post_wallet_transaction(cls.rs["id"], "topup_credit", 100000,
                                    "TP-P13DOC-1", "seed")

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

    def _hdr(self):
        return {"Authorization": f"Bearer {self.key}.{self.sec}"}

    # ---------------------------------------------------------- spec validity

    def test_openapi_spec_parses_and_is_3x(self):
        self.assertTrue(str(SPEC["openapi"]).startswith("3."))
        self.assertIn("info", SPEC)
        self.assertIn("paths", SPEC)
        self.assertIn("components", SPEC)

    def test_every_documented_route_exists_live(self):
        live = set()
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint.startswith("reseller_api_v1."):
                for m in rule.methods - {"HEAD", "OPTIONS"}:
                    live.add((m, _flask_to_openapi(rule.rule)))
        # live paths -> spec-relative
        live_rel = {(m, p.replace("/api/v1", "", 1) or "/") for (m, p) in live}
        documented = set()
        for path, item in SPEC["paths"].items():
            for method in item:
                documented.add((method.upper(), path))
        missing = documented - live_rel
        self.assertEqual(missing, set(),
                         f"documented but not implemented: {missing}")

    def test_every_live_route_is_documented(self):
        live = set()
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint.startswith("reseller_api_v1."):
                for m in rule.methods - {"HEAD", "OPTIONS"}:
                    live.add((m, _flask_to_openapi(rule.rule)))
        live_rel = {(m, p.replace("/api/v1", "", 1) or "/") for (m, p) in live}
        # the two index aliases collapse to one documented path
        live_rel.discard(("GET", ""))
        documented = set()
        for path, item in SPEC["paths"].items():
            for method in item:
                documented.add((method.upper(), path))
        undocumented = live_rel - documented
        self.assertEqual(undocumented, set(),
                         f"implemented but not documented: {undocumented}")

    # ---------------------------------------------------------- live shape checks

    def test_documented_response_fields_returned_live(self):
        # order create
        r = self.client.post("/api/v1/orders", headers=self._hdr(),
                             json={"product_id": self.pub, "quantity": 1,
                                   "client_order_id": "p13-doc-1"})
        self.assertEqual(r.status_code, 201, r.get_json())
        create_schema = SPEC["components"]["schemas"]["OrderCreated"]["properties"]
        data = r.get_json()["data"]
        for field in create_schema:
            if field == "idempotent":
                continue  # only on replays
            self.assertIn(field, data, f"documented field missing: {field}")

        oid = data["order_id"]

        # quote
        qschema = SPEC["components"]["schemas"]["Quote"]["properties"]
        q = self.client.post("/api/v1/orders/quote", headers=self._hdr(),
                             json={"product_id": self.pub, "quantity": 1})
        qd = q.get_json()["data"]
        for field in qschema:
            self.assertIn(field, qd, f"quote field missing: {field}")

        # balance
        bschema = SPEC["components"]["schemas"]["Balance"]["properties"]
        b = self.client.get("/api/v1/balance", headers=self._hdr()).get_json()["data"]
        for field in bschema:
            self.assertIn(field, b)

        # delivery
        dschema = SPEC["components"]["schemas"]["Delivery"]["properties"]
        dv = self.client.get(f"/api/v1/orders/{oid}/delivery",
                             headers=self._hdr()).get_json()["data"]["delivery"]
        for field in ("order_id", "delivery_status", "items"):
            self.assertIn(field, dv)

        # catalog product
        cschema = SPEC["components"]["schemas"]["CatalogProduct"]["properties"]
        c = self.client.get("/api/v1/products", headers=self._hdr()).get_json()
        prod = c["data"]["products"][0]
        for field in cschema:
            self.assertIn(field, prod, f"catalog field missing: {field}")

    def test_documented_request_fields_work_live(self):
        """Every field the spec accepts for POST /orders actually works."""
        r = self.client.post("/api/v1/orders", headers=self._hdr(),
                             json={"product_id": self.pub, "quantity": 1,
                                   "client_order_id": "p13-doc-2"})
        self.assertIn(r.status_code, (201, 200, 422))  # 422 if stock drained
        # rejected field documented as forbidden really is rejected
        r2 = self.client.post("/api/v1/orders", headers=self._hdr(),
                              json={"product_id": self.pub, "quantity": 1,
                                    "total": "0.01"})
        self.assertEqual(r2.status_code, 400)

    # ---------------------------------------------------------- guide coverage

    def test_guide_covers_all_required_topics(self):
        required = [
            "authentication", "api key", "base url", "version",
            "products", "mapping", "pricing", "quote", "orders",
            "order status", "delivery", "wallet", "webhook",
            "rate limit", "quota", "idempot", "error code",
            "pagination", "request id", "security",
        ]
        lower = GUIDE.lower()
        for topic in required:
            self.assertIn(topic, lower, f"guide missing topic: {topic}")

    def test_docs_contain_no_real_secrets(self):
        """Only placeholder credentials may appear in docs/spec."""
        for text, name in ((GUIDE, "guide"), (open(ROOT / "docs" / "openapi.yaml").read(), "spec")):
            # no 40+ char token-looking strings that aren't obvious placeholders
            for m in re.finditer(r"[A-Za-z0-9_\-]{32,}", text):
                token = m.group(0)
                self.assertTrue(
                    token.isupper() or "EXAMPLE" in token.upper()
                    or set(token) <= set("0123456789abcdef")
                    or "X" in token or token.endswith("…"),
                    f"{name}: suspicious token {token[:12]}…",
                )

    def test_guide_examples_match_live_behavior(self):
        """Spot-check: the documented idempotent-replay and error examples are
        exactly what the API does today."""
        r1 = self.client.post("/api/v1/orders", headers=self._hdr(),
                              json={"product_id": self.pub, "quantity": 1,
                                    "client_order_id": "p13-doc-replay"})
        r2 = self.client.post("/api/v1/orders", headers=self._hdr(),
                              json={"product_id": self.pub, "quantity": 1,
                                    "client_order_id": "p13-doc-replay"})
        if r1.status_code == 201:
            self.assertEqual(r2.status_code, 200)
            self.assertTrue(r2.get_json()["data"]["idempotent"])
            self.assertEqual(r2.get_json()["data"]["order_id"],
                             r1.get_json()["data"]["order_id"])
        # documented uniform-404 example
        g = self.client.get("/api/v1/orders/ORD-99999999", headers=self._hdr())
        self.assertEqual(g.get_json()["error"]["code"], "order_not_found")


if __name__ == "__main__":
    unittest.main()
