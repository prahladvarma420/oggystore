import ast
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_fd, _db_path = tempfile.mkstemp(prefix="oggy_product_security_", suffix=".db")
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
from reseller_api.services.credentials import parse_authorization_header
from reseller_api.services.product_catalog import (
    MAX_OFFSET, ProductCatalogError, get_product, get_products,
    parse_product_detail_request, parse_product_filters,
)


class ProductApiSecurityReviewTests(unittest.TestCase):
    counter = 0

    def setUp(self):
        type(self).counter += 1
        self.uid = 1800000 + self.counter
        db.cur.execute(
            "INSERT INTO users(user_id,first_name,username,blocked) VALUES(?,?,?,0)",
            (self.uid, "Security", f"product_security_{self.counter}"),
        )
        db.conn.commit()
        self.reseller = rdb.create_reseller(
            f"Security {self.counter}", self.uid, f"product_security_{self.counter}"
        )
        rdb.set_reseller_status(self.reseller["id"], "active")
        self.key_id, self.secret = rdb.create_api_key(self.reseller["id"], "test")

    @classmethod
    def tearDownClass(cls):
        try: db.close_thread_connection()
        finally:
            try: os.unlink(_db_path)
            except FileNotFoundError: pass

    def context(self):
        p = authenticate_credentials(self.key_id, self.secret, "test")
        self.assertIsNotNone(p)
        return ApiRequestContext("d" * 32, p.environment, p.reseller_id,
                                 p.reseller_public_id, p.api_key_id,
                                 p.api_key_public_id)

    def product(self, name="Product", active=1, description="Description",
                delivery="PRIVATE-DELIVERY", validity="1 year", warranty="30 days"):
        db.cur.execute(
            "INSERT INTO products(name,price,stock,delivery,active,description,"
            "validity,warranty,max_per_order) VALUES(?,?,?,?,?,?,?,?,?)",
            (name, 10.0, 3, delivery, active, description, validity, warranty, 5),
        )
        db.conn.commit()
        pid = int(db.cur.lastrowid)
        return pid, rdb.ensure_product_public_id(pid)

    def grant(self, pid, reseller_id=None, enabled=True):
        rdb.set_product_access(reseller_id or self.reseller["id"], pid, enabled)

    def detail(self, public_id):
        return get_product(self.context(), parse_product_detail_request(public_id, {}), "test")

    def not_found(self, public_id):
        with self.assertRaises(ProductCatalogError) as caught:
            self.detail(public_id)
        return caught.exception

    def test_idor_another_resellers_product_and_reseller_id_manipulation(self):
        other_uid = self.uid + 2000000
        db.cur.execute(
            "INSERT INTO users(user_id,first_name,username,blocked) VALUES(?,?,?,0)",
            (other_uid, "Other", f"other_security_{self.counter}"),
        )
        db.conn.commit()
        other = rdb.create_reseller("Other", other_uid, f"other_security_{self.counter}")
        pid, public_id = self.product("Other Product")
        self.grant(pid, reseller_id=other["id"])
        self.assertEqual(self.not_found(public_id).code, "product_not_found")
        for args in ({"reseller_id": str(other["id"])}, {"reseller": other["public_id"]}):
            with self.assertRaises(ProductCatalogError) as caught:
                parse_product_filters(args)
            self.assertEqual(caught.exception.code, "invalid_parameter")

    def test_product_id_manipulation_and_unauthorized_access(self):
        pid, public_id = self.product("Denied")
        attempts = (str(pid), public_id.lower(), public_id + "' OR 1=1--",
                    "../" + public_id, public_id + "/extra", "%00" + public_id)
        for value in attempts:
            with self.subTest(value=value), self.assertRaises(ProductCatalogError) as caught:
                parse_product_detail_request(value, {})
            self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.not_found(public_id).code, "product_not_found")

    def test_inactive_and_inaccessible_products_are_indistinguishable_from_missing(self):
        denied_pid, denied_id = self.product("Denied")
        inactive_pid, inactive_id = self.product("Inactive", active=0)
        self.grant(inactive_pid)
        errors = [self.not_found(denied_id), self.not_found(inactive_id),
                  self.not_found("PRD-ABCDEFGH2345")]
        self.assertEqual(
            {(e.code, e.message, e.status) for e in errors},
            {("product_not_found", "The requested product was not found.", 404)},
        )
        self.assertNotEqual(denied_pid, inactive_pid)

    def test_price_total_and_cost_manipulation_are_rejected(self):
        pid, public_id = self.product("Priced")
        self.grant(pid)
        rdb.set_pricing_rule(self.reseller["id"], pid, "fixed_price", 725)
        self.assertEqual(self.detail(public_id)["price"], "7.25")
        for field in ("price", "unit_price", "reseller_price", "total", "cost"):
            with self.subTest(field=field), self.assertRaises(ProductCatalogError):
                parse_product_detail_request(public_id, {field: "0.01"})
            with self.subTest(list_field=field), self.assertRaises(ProductCatalogError):
                parse_product_filters({field: "0.01"})

    def test_sql_and_search_filter_injection_are_bound_and_harmless(self):
        pid, _public_id = self.product("Normal Product")
        self.grant(pid)
        payloads = ("' OR 1=1--", "%'; DROP TABLE products;--", "_ OR TRUE", "\\x00")
        for payload in payloads:
            with self.subTest(payload=payload):
                products, _meta = get_products(
                    self.context(), parse_product_filters({"search": payload})
                )
                self.assertEqual(products, [])
                products, _meta = get_products(
                    self.context(), parse_product_filters({"category": payload})
                )
                self.assertEqual(products, [])
        db.cur.execute("SELECT COUNT(*) FROM products WHERE id=?", (pid,))
        self.assertEqual(db.cur.fetchone()[0], 1)

    def test_nosql_style_objects_and_parameter_pollution_are_rejected(self):
        for value in ({"$ne": ""}, ["x", "y"], ("x", "y")):
            with self.subTest(value=value), self.assertRaises(ProductCatalogError):
                parse_product_filters({"search": value})
        class Multi:
            def keys(self): return ["search"]
            def get(self, key, default=None): return "safe"
            def getlist(self, key): return ["safe", "' OR 1=1--"]
        with self.assertRaises(ProductCatalogError) as caught:
            parse_product_filters(Multi())
        self.assertEqual(caught.exception.code, "invalid_parameter")

    def test_excessive_pagination_is_rejected(self):
        attempts = (
            {"limit": "101"}, {"page": "100001"},
            {"page": str((MAX_OFFSET // 100) + 2), "limit": "100"},
            {"page": "-1"}, {"limit": "all"},
        )
        for args in attempts:
            with self.subTest(args=args), self.assertRaises(ProductCatalogError) as caught:
                parse_product_filters(args)
            self.assertEqual(caught.exception.code, "invalid_pagination")

    def test_response_size_and_text_fields_are_bounded(self):
        huge = "X" * 20000
        for i in range(100):
            pid, _ = self.product(f"Product {i} " + huge, description=huge,
                                  validity=huge, warranty=huge)
            self.grant(pid)
        products, meta = get_products(
            self.context(), parse_product_filters({"limit": "100"})
        )
        self.assertEqual(len(products), 100)
        self.assertTrue(all(len(p["name"]) <= 200 for p in products))
        self.assertTrue(all(len(p["description"]) <= 1000 for p in products))
        self.assertTrue(all(len(p["validity"]) <= 250 for p in products))
        self.assertLess(len(json.dumps({"products": products, "meta": meta})), 200000)

    def test_api_key_bypass_and_modified_headers_are_rejected(self):
        valid = f"Bearer {self.key_id}.{self.secret}"
        attempts = ("", "Basic " + valid[7:], valid + ".extra",
                    valid + "\r\nX-Reseller-ID: 1", "Bearer  " + valid[7:] + " extra",
                    "Bearer rk_test_ABCDEFGH." + "z" * 43)
        for value in attempts:
            with self.subTest(value=value[:50]):
                parsed = parse_authorization_header(value)
                if parsed:
                    self.assertIsNone(authenticate_credentials(parsed[0], parsed[1], "test"))
                else:
                    self.assertIsNone(parsed)

    def test_revoked_suspended_and_blocked_access_are_rejected(self):
        self.assertIsNotNone(authenticate_credentials(self.key_id, self.secret, "test"))
        rdb.revoke_api_key_for_reseller(self.reseller["id"], self.key_id)
        self.assertIsNone(authenticate_credentials(self.key_id, self.secret, "test"))
        key2, secret2 = rdb.create_api_key(self.reseller["id"], "test")
        rdb.set_reseller_status(self.reseller["id"], "suspended")
        self.assertIsNone(authenticate_credentials(key2, secret2, "test"))
        rdb.set_reseller_status(self.reseller["id"], "active")
        db.cur.execute("UPDATE users SET blocked=1 WHERE user_id=?", (self.uid,))
        db.conn.commit()
        self.assertIsNone(authenticate_credentials(key2, secret2, "test"))

    def test_sensitive_fields_and_internal_ids_do_not_leak(self):
        pid, public_id = self.product("Safe", delivery="SUPER-SECRET-CREDENTIAL")
        self.grant(pid)
        item = self.detail(public_id)
        self.assertEqual(set(item), {
            "id", "name", "description", "category", "price", "currency",
            "availability", "available_quantity", "max_per_order", "validity",
            "warranty", "featured",
        })
        rendered = json.dumps(item)
        self.assertNotIn("SUPER-SECRET-CREDENTIAL", rendered)
        self.assertNotIn("delivery", rendered.lower())
        self.assertNotIn("cost", rendered.lower())
        self.assertNotIn("product_id", rendered)
        self.assertNotEqual(item["id"], str(pid))

    def test_stack_traces_and_sql_errors_use_generic_global_handler(self):
        source = (ROOT / "reseller_api" / "errors.py").read_text()
        ast.parse(source)
        handler = source[source.index("@app.errorhandler(Exception)"):]
        self.assertIn('"internal_error"', handler)
        self.assertIn('"An unexpected error occurred."', handler)
        self.assertNotIn("str(exc)", handler)
        self.assertNotIn("traceback", handler.lower())
        self.assertNotIn("SELECT ", handler)


if __name__ == "__main__":
    unittest.main(verbosity=2)
