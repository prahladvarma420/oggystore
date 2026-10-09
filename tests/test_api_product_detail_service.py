import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_fd, _db_path = tempfile.mkstemp(prefix="oggy_product_detail_", suffix=".db")
os.close(_fd)

import config
config.DB_PATH = _db_path
import database.db as db
db.DB_PATH = _db_path
db._tls = threading.local()
db.init_db()
import database.reseller_db as rdb
from reseller_api.context import ApiRequestContext
from reseller_api.services.authentication import authenticate_credentials
from reseller_api.services.authorization import PRODUCTS_READ, authorize_operation
from reseller_api.services.product_catalog import (
    ProductCatalogError, get_product, parse_product_detail_request,
)


class ProductDetailServiceTests(unittest.TestCase):
    counter = 0

    def setUp(self):
        type(self).counter += 1
        self.uid = 1500000 + self.counter
        db.cur.execute(
            "INSERT INTO users(user_id,first_name,username,blocked) VALUES(?,?,?,0)",
            (self.uid, "Detail", f"detail_{self.counter}"),
        )
        db.conn.commit()
        self.reseller = rdb.create_reseller(
            f"Detail {self.counter}", self.uid, f"detail_{self.counter}"
        )
        rdb.set_reseller_status(self.reseller["id"], "active")
        self.key_id, self.secret = rdb.create_api_key(self.reseller["id"], "test")

    @classmethod
    def tearDownClass(cls):
        try:
            db.close_thread_connection()
        finally:
            try: os.unlink(_db_path)
            except FileNotFoundError: pass

    def principal(self):
        return authenticate_credentials(self.key_id, self.secret, "test")

    def context(self):
        p = self.principal()
        self.assertIsNotNone(p)
        return ApiRequestContext("b" * 32, p.environment, p.reseller_id,
                                 p.reseller_public_id, p.api_key_id,
                                 p.api_key_public_id)

    def create_product(self, name="Product", active=1):
        db.cur.execute(
            "INSERT INTO products(name,price,stock,delivery,active,description,"
            "validity,warranty,max_per_order) VALUES(?,?,?,?,?,?,?,?,?)",
            (name, 10.0, 4, "PRIVATE-SECRET", active, "Description",
             "1 year", "30 days", 5),
        )
        db.conn.commit()
        pid = int(db.cur.lastrowid)
        return pid, rdb.ensure_product_public_id(pid)

    def grant(self, pid, reseller_id=None, enabled=True):
        rdb.set_product_access(reseller_id or self.reseller["id"], pid, enabled)

    def detail(self, public_id):
        ctx = self.context()
        self.assertTrue(authorize_operation(ctx, PRODUCTS_READ, "test").allowed)
        parsed = parse_product_detail_request(public_id, {})
        return get_product(ctx, parsed, "test")

    def assert_not_found(self, public_id):
        with self.assertRaises(ProductCatalogError) as caught:
            self.detail(public_id)
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "product_not_found")
        self.assertEqual(caught.exception.message, "The requested product was not found.")

    def test_valid_product(self):
        pid, public_id = self.create_product("<b>Safe Product</b>")
        self.grant(pid)
        item = self.detail(public_id)
        self.assertEqual(item["id"], public_id)
        self.assertEqual(item["name"], "Safe Product")
        self.assertNotIn("product_id", item)
        self.assertNotIn("delivery", item)
        self.assertNotIn("PRIVATE-SECRET", repr(item))

    def test_invalid_product(self):
        self.assert_not_found("PRD-ABCDEFGH2345")

    def test_inaccessible_product(self):
        _pid, public_id = self.create_product("No Access")
        self.assert_not_found(public_id)

    def test_inactive_product(self):
        pid, public_id = self.create_product("Inactive", active=0)
        self.grant(pid)
        self.assert_not_found(public_id)

    def test_valid_reseller(self):
        ctx = self.context()
        self.assertTrue(authorize_operation(ctx, PRODUCTS_READ, "test").allowed)

    def test_invalid_api_key(self):
        self.assertIsNone(authenticate_credentials("rk_test_ABCDEFGH", "z" * 43, "test"))

    def test_revoked_key(self):
        rdb.revoke_api_key_for_reseller(self.reseller["id"], self.key_id)
        self.assertIsNone(self.principal())

    def test_suspended_reseller(self):
        rdb.set_reseller_status(self.reseller["id"], "suspended")
        self.assertIsNone(self.principal())

    def test_blocked_reseller(self):
        db.cur.execute("UPDATE users SET blocked=1 WHERE user_id=?", (self.uid,))
        db.conn.commit()
        self.assertIsNone(self.principal())

    def test_reseller_price(self):
        pid, public_id = self.create_product("Priced")
        self.grant(pid)
        rdb.set_pricing_rule(self.reseller["id"], pid, "fixed_price", 675)
        item = self.detail(public_id)
        self.assertEqual(item["price"], "6.75")
        self.assertEqual(item["currency"], "USDT")
        for field in ("price", "total", "cost"):
            with self.subTest(field=field), self.assertRaises(ProductCatalogError):
                parse_product_detail_request(public_id, {field: "0.01"})

    def test_idor_attempt(self):
        other_uid = self.uid + 1000000
        db.cur.execute(
            "INSERT INTO users(user_id,first_name,username,blocked) VALUES(?,?,?,0)",
            (other_uid, "Other", f"other_detail_{self.counter}"),
        )
        db.conn.commit()
        other = rdb.create_reseller("Other", other_uid, f"other_detail_{self.counter}")
        pid, public_id = self.create_product("Other Product")
        self.grant(pid, reseller_id=other["id"])
        self.assert_not_found(public_id)
        with self.assertRaises(ProductCatalogError) as caught:
            parse_product_detail_request(public_id, {"reseller_id": str(other["id"])})
        self.assertEqual(caught.exception.code, "invalid_parameter")

    def test_product_id_manipulation(self):
        attempts = ("1", "-1", "PRD-../../etc", "prd-abcdefgh2345",
                    "PRD-ABCDEFGH2345%00", "PRD-ABCDEFGH2345/extra", "")
        for value in attempts:
            with self.subTest(value=value), self.assertRaises(ProductCatalogError) as caught:
                parse_product_detail_request(value, {})
            self.assertEqual(caught.exception.status, 404)
            self.assertEqual(caught.exception.code, "product_not_found")


if __name__ == "__main__":
    unittest.main(verbosity=2)
