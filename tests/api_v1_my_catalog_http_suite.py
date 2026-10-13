"""
Phase 11 Prompt 4 — HTTP-layer tests for /api/v1/my-products.
Requires Flask.  Loaded conditionally by test_phase11_product_customization.py.
"""
from __future__ import annotations

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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_my_catalog_http_", suffix=".db")
os.close(_fd)

import config
config.DB_PATH = _db_path
import database.db as db
db.DB_PATH = _db_path
db._tls = threading.local()
db.init_db()

import database.reseller_db as rdb
from reseller_api import create_app
import reseller_api.middleware.rate_limit
import reseller_api.middleware.request_context
import reseller_api.middleware.security_controls
from reseller_api.services.rate_limit import RequestRateDecision

_ctr = 0


def _uid():
    global _ctr
    _ctr += 1
    return 7_400_000 + _ctr


def _make_reseller():
    uid = _uid()
    n = _ctr
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"MyCatRS{n}", f"mycat_rs_{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"MyCatReseller {n}", uid, f"mycat_user_{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs["id"], key_id, secret


def _make_product(name="HTTP Widget", price=9.99, active=1):
    n = _ctr + 1
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active, description,"
        " validity, warranty, rules, redeem_link, max_per_order)"
        " VALUES(?,?,30,'code_stock',?,?,?,?,?,?,?)",
        (name, price, active, f"Desc {name}{n}", "30d", "1y", "none", "no", 3),
    )
    db.conn.commit()
    pid = db.cur.lastrowid
    pub = rdb.ensure_product_public_id(pid)
    return pid, pub


def _grant(reseller_id, product_id):
    rdb.set_product_access(reseller_id, product_id, enabled=True)


def _import(reseller_id, product_id):
    rdb.import_reseller_product(reseller_id, product_id)


class MyCatalogHttpTests(unittest.TestCase):
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

    def setUp(self):
        self.pid, self.pub = _make_product()
        self.rs1, self.k1, self.s1 = _make_reseller()
        _grant(self.rs1, self.pid)
        _import(self.rs1, self.pid)
        self.rs2, self.k2, self.s2 = _make_reseller()
        _grant(self.rs2, self.pid)
        _import(self.rs2, self.pid)

    def _hdr(self, k, s):
        return {"Authorization": f"Bearer {k}.{s}"}

    def _patch(self, pub, body, k=None, s=None):
        return self.client.patch(
            f"/api/v1/my-products/{pub}",
            json=body,
            headers=self._hdr(k or self.k1, s or self.s1),
        )

    def _get_one(self, pub, k=None, s=None):
        return self.client.get(
            f"/api/v1/my-products/{pub}",
            headers=self._hdr(k or self.k1, s or self.s1),
        )

    def _get_list(self, k=None, s=None):
        return self.client.get(
            "/api/v1/my-products",
            headers=self._hdr(k or self.k1, s or self.s1),
        )

    def _delete(self, pub, k=None, s=None):
        return self.client.delete(
            f"/api/v1/my-products/{pub}",
            headers=self._hdr(k or self.k1, s or self.s1),
        )

    # GET list
    def test_list_my_products(self):
        r = self._get_list()
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("products", data)
        pids = [p["product_id"] for p in data["products"]]
        self.assertIn(self.pub, pids)

    def test_list_no_auth(self):
        r = self.client.get("/api/v1/my-products")
        self.assertIn(r.status_code, (401, 403))

    def test_list_reseller_isolation(self):
        self._patch(self.pub, {"custom_name": "RS1 Only"}, self.k1, self.s1)
        r1 = self._get_list(self.k1, self.s1)
        r2 = self._get_list(self.k2, self.s2)
        names1 = [p.get("custom_name") for p in r1.get_json()["products"]]
        names2 = [p.get("custom_name") for p in r2.get_json()["products"]]
        self.assertIn("RS1 Only", names1)
        self.assertNotIn("RS1 Only", names2)

    # GET one
    def test_get_my_product(self):
        r = self._get_one(self.pub)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["product_id"], self.pub)

    def test_get_not_found(self):
        r = self._get_one("PRD-AAAAAAAAAA00")
        self.assertEqual(r.status_code, 404)

    def test_get_no_auth(self):
        r = self.client.get(f"/api/v1/my-products/{self.pub}")
        self.assertIn(r.status_code, (401, 403))

    # PATCH
    def test_rename(self):
        r = self._patch(self.pub, {"custom_name": "My Widget"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["custom_name"], "My Widget")

    def test_rename_master_unchanged(self):
        self._patch(self.pub, {"custom_name": "Reseller Name"})
        prod = rdb.get_product_by_id(self.pid)
        self.assertEqual(prod["name"], "HTTP Widget")

    def test_rename_other_reseller_unchanged(self):
        self._patch(self.pub, {"custom_name": "RS1 Name"}, self.k1, self.s1)
        r2 = self._get_one(self.pub, self.k2, self.s2)
        self.assertIsNone(r2.get_json()["custom_name"])

    def test_description_change(self):
        r = self._patch(self.pub, {"custom_description": "Great deal!"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["custom_description"], "Great deal!")

    def test_sku_change(self):
        r = self._patch(self.pub, {"reseller_sku": "SKU-HTTP-001"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["reseller_sku"], "SKU-HTTP-001")

    def test_disable(self):
        r = self._patch(self.pub, {"enabled": False})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["enabled"])

    def test_enable(self):
        self._patch(self.pub, {"enabled": False})
        r = self._patch(self.pub, {"enabled": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["enabled"])

    def test_disable_master_unchanged(self):
        self._patch(self.pub, {"enabled": False})
        prod = rdb.get_product_by_id(self.pid)
        self.assertTrue(bool(prod["active"]))

    def test_disable_other_reseller_unchanged(self):
        self._patch(self.pub, {"enabled": False}, self.k1, self.s1)
        r2 = self._get_one(self.pub, self.k2, self.s2)
        self.assertTrue(r2.get_json()["enabled"])

    def test_cannot_patch_price(self):
        r = self._patch(self.pub, {"price": 0.01})
        self.assertIn(r.status_code, (400, 422))

    def test_cannot_patch_stock(self):
        r = self._patch(self.pub, {"stock": 0})
        self.assertIn(r.status_code, (400, 422))

    def test_unknown_field_rejected(self):
        r = self._patch(self.pub, {"bogus": "x"})
        self.assertIn(r.status_code, (400, 422))

    def test_empty_body_rejected(self):
        r = self._patch(self.pub, {})
        self.assertIn(r.status_code, (400, 422))

    def test_patch_not_found(self):
        r = self._patch("PRD-AAAAAAAAAA00", {"custom_name": "X"})
        self.assertEqual(r.status_code, 404)

    def test_patch_no_auth(self):
        r = self.client.patch(
            f"/api/v1/my-products/{self.pub}", json={"custom_name": "Hack"}
        )
        self.assertIn(r.status_code, (401, 403))

    def test_patch_cannot_modify_other_reseller(self):
        self._patch(self.pub, {"custom_name": "RS2 Name"}, self.k2, self.s2)
        r1 = self._get_one(self.pub, self.k1, self.s1)
        self.assertIsNone(r1.get_json()["custom_name"])

    # DELETE
    def test_delete(self):
        r = self._delete(self.pub)
        self.assertEqual(r.status_code, 204)
        r2 = self._get_one(self.pub)
        self.assertEqual(r2.status_code, 404)

    def test_delete_master_unchanged(self):
        self._delete(self.pub)
        prod = rdb.get_product_by_id(self.pid)
        self.assertIsNotNone(prod)

    def test_delete_other_reseller_unchanged(self):
        self._delete(self.pub, self.k1, self.s1)
        r2 = self._get_one(self.pub, self.k2, self.s2)
        self.assertEqual(r2.status_code, 200)

    def test_delete_not_found(self):
        r = self._delete("PRD-AAAAAAAAAA00")
        self.assertEqual(r.status_code, 404)

    def test_delete_no_auth(self):
        r = self.client.delete(f"/api/v1/my-products/{self.pub}")
        self.assertIn(r.status_code, (401, 403))

    # Deleted master
    def test_patch_after_master_deleted(self):
        rdb.delete_product(self.pid)
        r = self._patch(self.pub, {"custom_name": "Ghost"})
        self.assertEqual(r.status_code, 404)

    def test_list_after_master_deleted(self):
        rdb.delete_product(self.pid)
        r = self._get_list()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_json()["products"]), 0)


if __name__ == "__main__":
    unittest.main()
