import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_fd, _db_path = tempfile.mkstemp(prefix="oggy_product_catalog_", suffix=".db")
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
    MAX_LIMIT,
    ProductCatalogError,
    get_products,
    parse_product_filters,
)


class ProductCatalogServiceTests(unittest.TestCase):
    counter = 0

    def setUp(self):
        type(self).counter += 1
        self.uid = 1200000 + self.counter
        db.cur.execute(
            "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
            (self.uid, "Catalog Test", f"catalog_service_{self.counter}"),
        )
        db.conn.commit()
        self.reseller = rdb.create_reseller(
            f"Catalog Service {self.counter}",
            self.uid,
            f"catalog_service_{self.counter}",
        )
        rdb.set_reseller_status(self.reseller["id"], "active")
        self.key_id, self.secret = rdb.create_api_key(self.reseller["id"], "test")

    @classmethod
    def tearDownClass(cls):
        try:
            db.close_thread_connection()
        finally:
            try:
                os.unlink(_db_path)
            except FileNotFoundError:
                pass

    def principal(self):
        return authenticate_credentials(self.key_id, self.secret, "test")

    def context(self):
        principal = self.principal()
        self.assertIsNotNone(principal)
        return ApiRequestContext(
            request_id="a" * 32,
            environment=principal.environment,
            reseller_id=principal.reseller_id,
            reseller_public_id=principal.reseller_public_id,
            api_key_id=principal.api_key_id,
            api_key_public_id=principal.api_key_public_id,
        )

    def filters(self, **values):
        return parse_product_filters({k: str(v) for k, v in values.items()})

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

    def grant(self, product_id, *, reseller_id=None, enabled=True):
        rdb.set_product_access(
            reseller_id or self.reseller["id"], product_id, enabled
        )

    def catalog(self, **filters):
        decision = authorize_operation(self.context(), PRODUCTS_READ, "test")
        self.assertTrue(decision.allowed, decision.reason)
        return get_products(self.context(), self.filters(**filters))

    # 1
    def test_valid_reseller_authenticates_and_is_authorized(self):
        context = self.context()
        decision = authorize_operation(context, PRODUCTS_READ, "test")
        self.assertTrue(decision.allowed)
        self.assertEqual(context.reseller_id, self.reseller["id"])

    # 2
    def test_invalid_api_key_is_rejected(self):
        self.assertIsNone(
            authenticate_credentials("rk_test_ABCDEFGH", "z" * 43, "test")
        )

    # 3
    def test_revoked_api_key_is_rejected(self):
        self.assertTrue(
            rdb.revoke_api_key_for_reseller(self.reseller["id"], self.key_id)
        )
        self.assertIsNone(authenticate_credentials(self.key_id, self.secret, "test"))

    # 4
    def test_suspended_reseller_is_rejected(self):
        rdb.set_reseller_status(self.reseller["id"], "suspended")
        self.assertIsNone(authenticate_credentials(self.key_id, self.secret, "test"))

    # 5
    def test_blocked_reseller_is_rejected(self):
        db.cur.execute("UPDATE users SET blocked=1 WHERE user_id=?", (self.uid,))
        db.conn.commit()
        self.assertIsNone(authenticate_credentials(self.key_id, self.secret, "test"))

    # 6
    def test_product_access_allowed_returns_only_safe_master_fields(self):
        product_id = self.create_product("<b>Allowed Product</b>")
        self.grant(product_id)
        products, meta = self.catalog()
        self.assertEqual(len(products), 1)
        item = products[0]
        self.assertEqual(item["name"], "Allowed Product")
        self.assertRegex(item["id"], r"^PRD-[A-HJ-NP-Z2-9]{12}$")
        self.assertEqual(item["availability"], "in_stock")
        self.assertEqual(item["available_quantity"], 3)
        self.assertNotIn("delivery", item)
        self.assertNotIn("product_id", item)
        self.assertNotIn("PRIVATE-DELIVERY-SECRET", repr(item))
        self.assertEqual(meta["total"], 1)
        again, _ = self.catalog()
        self.assertEqual(again[0]["id"], item["id"])

    # 7
    def test_product_access_denied_and_missing_grant_are_hidden(self):
        disabled = self.create_product("Disabled Grant")
        missing = self.create_product("Missing Grant")
        self.grant(disabled, enabled=False)
        products, meta = self.catalog()
        self.assertEqual(products, [])
        self.assertEqual(meta["total"], 0)
        self.assertNotEqual(disabled, missing)

    # 8
    def test_inactive_product_is_hidden_even_when_granted(self):
        product_id = self.create_product("Inactive", active=0)
        self.grant(product_id)
        products, meta = self.catalog()
        self.assertEqual(products, [])
        self.assertEqual(meta["total"], 0)
        with self.assertRaises(ProductCatalogError) as caught:
            parse_product_filters({"active": "false"})
        self.assertEqual(caught.exception.code, "invalid_filter")

    # 9
    def test_pagination_has_predictable_metadata_and_stable_order(self):
        for name in ("One", "Two", "Three"):
            product_id = self.create_product(name)
            self.grant(product_id)
        first, meta1 = self.catalog(page=1, limit=1)
        second, meta2 = self.catalog(page=2, limit=1)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(meta1, {
            "page": 1, "limit": 1, "total": 3, "pages": 3,
            "has_next": True, "has_previous": False,
        })
        self.assertTrue(meta2["has_previous"])

    # 10
    def test_maximum_page_size_is_accepted_and_capped(self):
        parsed = parse_product_filters({"limit": str(MAX_LIMIT)})
        self.assertEqual(parsed["limit"], 100)
        _products, meta = get_products(self.context(), parsed)
        self.assertEqual(meta["limit"], 100)

    # 11
    def test_invalid_page_size_is_rejected(self):
        for raw in ("0", "101", "all", "-1", ""):
            with self.subTest(raw=raw), self.assertRaises(ProductCatalogError) as caught:
                parse_product_filters({"limit": raw})
            self.assertEqual(caught.exception.code, "invalid_pagination")

    # 12
    def test_search_matches_name_description_and_escapes_wildcards(self):
        rows = (
            self.create_product("Cloud Mail", description="Business account"),
            self.create_product("Streaming", description="Entertainment"),
            self.create_product("100% Genuine", description="Percent sign"),
        )
        for product_id in rows:
            self.grant(product_id)
        products, _ = self.catalog(search="business")
        self.assertEqual([p["name"] for p in products], ["Cloud Mail"])
        products, _ = self.catalog(search="%")
        self.assertEqual([p["name"] for p in products], ["100% Genuine"])
        with self.assertRaises(ProductCatalogError):
            parse_product_filters({"search": "x" * 101})

    # 13
    def test_category_filter_uses_existing_active_categories(self):
        active_category = self.create_category("Subscriptions")
        inactive_category = self.create_category("Hidden", active=0)
        first = self.create_product("Subscription", category_id=active_category)
        second = self.create_product("Hidden Product", category_id=inactive_category)
        self.grant(first)
        self.grant(second)
        products, _ = self.catalog(category="subscriptions")
        self.assertEqual([p["name"] for p in products], ["Subscription"])
        self.assertEqual(products[0]["category"], "Subscriptions")
        hidden, _ = self.catalog(category="hidden")
        self.assertEqual(hidden, [])

    # 14
    def test_reseller_pricing_is_calculated_server_side(self):
        product_id = self.create_product("Contract Price")
        self.grant(product_id)
        rdb.set_pricing_rule(
            self.reseller["id"], product_id, "fixed_price", 725
        )
        products, _ = self.catalog()
        self.assertEqual(products[0]["price"], "7.25")
        self.assertEqual(products[0]["currency"], "USDT")
        with self.assertRaises(ProductCatalogError) as caught:
            parse_product_filters({"price": "0.01"})
        self.assertEqual(caught.exception.code, "invalid_filter")

    # 15
    def test_reseller_id_manipulation_is_rejected_and_cannot_expand_access(self):
        other_uid = self.uid + 1000000
        db.cur.execute(
            "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
            (other_uid, "Other", f"other_service_{self.counter}"),
        )
        db.conn.commit()
        other = rdb.create_reseller(
            "Other", other_uid, f"other_service_{self.counter}"
        )
        product_id = self.create_product("Other Catalog")
        self.grant(product_id, reseller_id=other["id"])
        products, _ = self.catalog()
        self.assertEqual(products, [])
        with self.assertRaises(ProductCatalogError) as caught:
            parse_product_filters({"reseller_id": str(other["id"])})
        self.assertEqual(caught.exception.code, "invalid_parameter")

    # 16
    def test_empty_product_result(self):
        products, meta = self.catalog()
        self.assertEqual(products, [])
        self.assertEqual(meta, {
            "page": 1, "limit": 20, "total": 0, "pages": 0,
            "has_next": False, "has_previous": False,
        })


if __name__ == "__main__":
    unittest.main(verbosity=2)
