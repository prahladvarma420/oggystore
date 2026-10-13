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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_api_products_", suffix=".db")
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


class ApiV1ProductsTests(unittest.TestCase):
    counter = 0

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
        type(self).counter += 1
        self.uid = 990000 + self.counter
        db.cur.execute(
            "INSERT INTO users(user_id, first_name, username, blocked) "
            "VALUES(?,?,?,0)",
            (self.uid, "Catalog Test", f"catalog_{self.counter}"),
        )
        db.conn.commit()
        self.reseller = rdb.create_reseller(
            f"Catalog Test {self.counter}", self.uid, f"catalog_{self.counter}"
        )
        rdb.set_reseller_status(self.reseller["id"], "active")
        self.key_id, self.secret = rdb.create_api_key(
            self.reseller["id"], "test"
        )

    def headers(self, key_id=None, secret=None):
        return {
            "Authorization": "Bearer {}.{}".format(
                key_id or self.key_id,
                self.secret if secret is None else secret,
            )
        }

    def create_category(self, name="Subscriptions", active=1):
        db.cur.execute(
            "INSERT INTO categories(name, icon, sort_order, active) VALUES(?,?,?,?)",
            (name, "", 1, active),
        )
        db.conn.commit()
        return int(db.cur.lastrowid)

    def create_product(self, name="Product", *, active=1, stock=3,
                       category_id=None, description="Description"):
        db.cur.execute(
            "INSERT INTO products(name, price, stock, delivery, active, "
            "description, validity, warranty, max_per_order, category_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                name, 10.00, stock, "PRIVATE-DELIVERY-SECRET", active,
                description, "1 year", "30 days", 5, category_id,
            ),
        )
        db.conn.commit()
        return int(db.cur.lastrowid)

    def grant(self, product_id, reseller_id=None, enabled=True):
        rdb.set_product_access(
            reseller_id or self.reseller["id"], product_id, enabled
        )

    def get_products(self, query="", headers=None):
        return self.client.get(
            "/api/v1/products" + query,
            headers=self.headers() if headers is None else headers,
        )

    def test_valid_reseller_gets_only_api_safe_fields_and_stable_public_id(self):
        product_id = self.create_product("<b>Safe Product</b>")
        self.grant(product_id)
        first = self.get_products()
        second = self.get_products()
        self.assertEqual(first.status_code, 200)
        item = first.get_json()["data"]["products"][0]
        self.assertEqual(item["name"], "Safe Product")
        self.assertRegex(item["id"], r"^PRD-[A-HJ-NP-Z2-9]{12}$")
        self.assertEqual(item["id"], second.get_json()["data"]["products"][0]["id"])
        self.assertEqual(set(item), {
            "id", "name", "description", "category", "price", "currency",
            "availability", "available_quantity", "max_per_order",
            "validity", "warranty", "featured", "status",
        })
        rendered = first.get_data(as_text=True)
        self.assertNotIn(str(product_id), item["id"])
        self.assertNotIn("PRIVATE-DELIVERY-SECRET", rendered)
        self.assertNotIn("delivery", rendered.lower())

    def test_invalid_api_key_is_rejected(self):
        response = self.get_products(
            headers=self.headers("rk_test_ABCDEFGH", "z" * 43)
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"]["code"], "authentication_failed")

    def test_revoked_api_key_is_rejected(self):
        rdb.revoke_api_key_for_reseller(self.reseller["id"], self.key_id)
        response = self.get_products()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"]["code"], "authentication_failed")

    def test_suspended_reseller_is_rejected(self):
        rdb.set_reseller_status(self.reseller["id"], "suspended")
        response = self.get_products()
        self.assertEqual(response.status_code, 401)

    def test_blocked_reseller_is_rejected(self):
        db.cur.execute("UPDATE users SET blocked=1 WHERE user_id=?", (self.uid,))
        db.conn.commit()
        response = self.get_products()
        self.assertEqual(response.status_code, 401)

    def test_access_allowed_denied_and_inactive_products(self):
        allowed = self.create_product("Allowed")
        denied = self.create_product("Denied")
        inactive = self.create_product("Inactive", active=0)
        self.grant(allowed)
        self.grant(denied, enabled=False)
        self.grant(inactive)
        response = self.get_products()
        self.assertEqual(response.status_code, 200)
        names = [p["name"] for p in response.get_json()["data"]["products"]]
        self.assertEqual(names, ["Allowed"])

    def test_pagination_and_maximum_page_size(self):
        for name in ("One", "Two", "Three"):
            product_id = self.create_product(name)
            self.grant(product_id)
        first = self.get_products("?page=1&limit=1")
        second = self.get_products("?page=2&limit=1")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(
            first.get_json()["data"]["products"][0]["id"],
            second.get_json()["data"]["products"][0]["id"],
        )
        meta = first.get_json()["meta"]["pagination"]
        self.assertEqual(meta, {
            "page": 1, "limit": 1, "total": 3, "pages": 3,
            "has_next": True, "has_previous": False,
        })
        maximum = self.get_products("?limit=100")
        self.assertEqual(maximum.status_code, 200)
        self.assertEqual(maximum.get_json()["meta"]["pagination"]["limit"], 100)

    def test_invalid_page_size_is_rejected(self):
        for query in ("?limit=0", "?limit=101", "?limit=all", "?page=-1"):
            with self.subTest(query=query):
                response = self.get_products(query)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.get_json()["error"]["code"], "invalid_pagination"
                )

    def test_search_is_bounded_and_escapes_wildcards(self):
        first = self.create_product("Cloud Mail", description="Business account")
        second = self.create_product("Streaming", description="Entertainment")
        percent = self.create_product("100% Genuine")
        for product_id in (first, second, percent):
            self.grant(product_id)
        response = self.get_products("?search=business")
        self.assertEqual(
            [p["name"] for p in response.get_json()["data"]["products"]],
            ["Cloud Mail"],
        )
        wildcard = self.get_products("?search=%25")
        self.assertEqual(
            [p["name"] for p in wildcard.get_json()["data"]["products"]],
            ["100% Genuine"],
        )
        too_long = self.get_products("?search=" + ("x" * 101))
        self.assertEqual(too_long.status_code, 400)

    def test_category_filtering_uses_active_master_category(self):
        subscriptions = self.create_category("Subscriptions")
        inactive_category = self.create_category("Hidden", active=0)
        subscription_product = self.create_product(
            "Subscription Product", category_id=subscriptions
        )
        hidden_product = self.create_product(
            "Hidden Category Product", category_id=inactive_category
        )
        for product_id in (subscription_product, hidden_product):
            self.grant(product_id)
        response = self.get_products("?category=subscriptions")
        products = response.get_json()["data"]["products"]
        self.assertEqual([p["name"] for p in products], ["Subscription Product"])
        self.assertEqual(products[0]["category"], "Subscriptions")
        hidden = self.get_products("?category=hidden")
        self.assertEqual(hidden.get_json()["data"]["products"], [])

    def test_reseller_price_is_resolved_server_side(self):
        product_id = self.create_product("Contract Price")
        self.grant(product_id)
        rdb.set_pricing_rule(
            self.reseller["id"], product_id, "fixed_price", 725
        )
        response = self.get_products()
        item = response.get_json()["data"]["products"][0]
        self.assertEqual(item["price"], "7.25")
        self.assertEqual(item["currency"], "USDT")
        manipulated = self.get_products("?price=0.01")
        self.assertEqual(manipulated.status_code, 400)
        self.assertEqual(manipulated.get_json()["error"]["code"], "invalid_filter")

    def test_reseller_id_manipulation_is_rejected_and_cannot_expand_access(self):
        other_uid = self.uid + 1000000
        db.cur.execute(
            "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
            (other_uid, "Other", f"other_{self.counter}"),
        )
        db.conn.commit()
        other = rdb.create_reseller("Other", other_uid, f"other_{self.counter}")
        product_id = self.create_product("Other Reseller Product")
        self.grant(product_id, reseller_id=other["id"])
        ordinary = self.get_products()
        self.assertEqual(ordinary.get_json()["data"]["products"], [])
        attempted = self.get_products(f"?reseller_id={other['id']}")
        self.assertEqual(attempted.status_code, 400)
        self.assertEqual(
            attempted.get_json()["error"]["code"], "invalid_parameter"
        )

    def test_empty_result_has_predictable_metadata(self):
        response = self.get_products()
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["data"]["products"], [])
        self.assertEqual(body["meta"]["pagination"], {
            "page": 1, "limit": 20, "total": 0, "pages": 0,
            "has_next": False, "has_previous": False,
        })
        self.assertRegex(body["request_id"], r"^[0-9a-f]{32}$")

    def test_product_status_filter_cannot_expose_inactive_rows(self):
        product_id = self.create_product("Inactive", active=0)
        self.grant(product_id)
        response = self.get_products("?active=false")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "invalid_filter")


if __name__ == "__main__":
    unittest.main(verbosity=2)
