"""HTTP regression tests for GET /api/v1/orders/<order_id>/delivery.

Written in Phase 11 Prompt 10 — the delivery endpoint had zero HTTP-level
coverage, which allowed three latent Phase-8 bugs to ship:
  1. DELIVERY_READ was missing from SUPPORTED_OPERATIONS (every call -> 403).
  2. The service checked a nonexistent context.permissions attribute.
  3. orders.master_status (and the other lifecycle columns) were never added
     to the schema (every call -> 500).

These tests exercise the REAL middleware stack, real DB, and real services —
only rate limiting, audit persistence, and the global kill-switch are stubbed.
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_api_delivery_", suffix=".db")
os.close(_fd)

import config
config.DB_PATH = _db_path
import database.db as db
db.DB_PATH = _db_path
db._tls = threading.local()
db.init_db()

import database.reseller_db as rdb
from reseller_api import create_app
from reseller_api.services.rate_limit import RequestRateDecision

_ctr = 0


def _make_reseller(name, status="active"):
    global _ctr
    _ctr += 1
    uid = 9_800_000 + _ctr
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, name, name.lower()),
    )
    db.conn.commit()
    rs = rdb.create_reseller(name, uid, name.lower())
    rdb.set_reseller_status(rs["id"], status)
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product_with_code(name, price=10.0, code="DELIV-CODE-1"):
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (name, price),
    )
    db.conn.commit()
    pid = db.cur.lastrowid
    pub = rdb.ensure_product_public_id(pid)
    db.cur.execute(
        "INSERT INTO product_stock(product_id, delivery, sold) VALUES(?,?,0)", (pid, code)
    )
    db.conn.commit()
    return pid, pub


class ApiV1DeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.audit_patcher = patch(
            "reseller_api.middleware.request_context.persist_audit_record",
            return_value=True,
        )
        cls.audit_patcher.start()
        cls.rate_patcher = patch(
            "reseller_api.middleware.rate_limit.evaluate_request_rate",
            return_value=RequestRateDecision(
                True, "allowed", limit=60, remaining=59, reset_after=45
            ),
        )
        cls.rate_patcher.start()
        cls.global_patcher = patch(
            "reseller_api.middleware.security_controls.is_global_api_enabled",
            return_value=True,
        )
        cls.global_patcher.start()
        cls.app = create_app({
            "TESTING": True,
            "API_ENVIRONMENT": "test",
            "INIT_DATABASE": False,
            "RESELLER_API_HSTS": False,
        })
        cls.client = cls.app.test_client()

        cls.rsA, cls.keyA, cls.secA = _make_reseller("DelivA")
        cls.rsB, cls.keyB, cls.secB = _make_reseller("DelivB")
        cls.pid, cls.pub = _make_product_with_code("Deliv Product")
        rdb.set_product_access(cls.rsA["id"], cls.pid, True)
        rdb.post_wallet_transaction(cls.rsA["id"], "topup_credit", 100000, "TP-DELIV-1", "seed")
        db.conn.commit()

        r = cls.client.post(
            "/api/v1/orders",
            headers=cls._hdr(cls.keyA, cls.secA),
            json={"product_id": cls.pub, "quantity": 1},
        )
        assert r.status_code == 201, r.get_json()
        cls.order_id = r.get_json()["data"]["order_id"]

    @classmethod
    def tearDownClass(cls):
        cls.global_patcher.stop()
        cls.rate_patcher.stop()
        cls.audit_patcher.stop()
        try:
            db.close_thread_connection()
        finally:
            try:
                os.unlink(_db_path)
            except FileNotFoundError:
                pass

    @staticmethod
    def _hdr(k, s):
        return {"Authorization": f"Bearer {k}.{s}"}

    def test_owner_gets_delivery(self):
        r = self.client.get(
            f"/api/v1/orders/{self.order_id}/delivery", headers=self._hdr(self.keyA, self.secA)
        )
        self.assertEqual(r.status_code, 200, r.get_json())
        data = r.get_json()["data"]["delivery"]
        self.assertEqual(data["order_id"], self.order_id)
        self.assertEqual(data["delivery_status"], "delivered")
        self.assertEqual(data["items"], ["DELIV-CODE-1"])

    def test_delivery_idor_other_reseller_404(self):
        r = self.client.get(
            f"/api/v1/orders/{self.order_id}/delivery", headers=self._hdr(self.keyB, self.secB)
        )
        self.assertEqual(r.status_code, 404)

    def test_delivery_unknown_order_404(self):
        r = self.client.get(
            "/api/v1/orders/ORD-99999999/delivery", headers=self._hdr(self.keyA, self.secA)
        )
        self.assertEqual(r.status_code, 404)

    def test_delivery_bad_order_id_400(self):
        r = self.client.get("/api/v1/orders/1/delivery", headers=self._hdr(self.keyA, self.secA))
        self.assertEqual(r.status_code, 400)

    def test_delivery_requires_auth(self):
        r = self.client.get(f"/api/v1/orders/{self.order_id}/delivery")
        self.assertEqual(r.status_code, 401)

    def test_delivery_wrong_secret_401(self):
        r = self.client.get(
            f"/api/v1/orders/{self.order_id}/delivery",
            headers={"Authorization": f"Bearer {self.keyA}.{'x' * 43}"},
        )
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
