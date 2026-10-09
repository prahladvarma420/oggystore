import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_fd, _db_path = tempfile.mkstemp(prefix="oggy_product_perf_", suffix=".db")
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
from reseller_api.services.product_catalog import (
    get_product, get_products, parse_product_filters,
)

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def public_id(number):
    chars = []
    value = int(number)
    for _ in range(12):
        chars.append(ALPHABET[value % len(ALPHABET)])
        value //= len(ALPHABET)
    return "PRD-" + "".join(reversed(chars))


class ProductApiPerformanceTests(unittest.TestCase):
    PRODUCT_COUNT = 5000

    @classmethod
    def setUpClass(cls):
        cls.uid = 7770001
        db.cur.execute(
            "INSERT INTO users(user_id,first_name,username,blocked) VALUES(?,?,?,0)",
            (cls.uid, "Performance", "performance"),
        )
        db.conn.commit()
        cls.reseller = rdb.create_reseller("Performance", cls.uid, "performance")
        rdb.set_reseller_status(cls.reseller["id"], "active")
        cls.key_id, cls.secret = rdb.create_api_key(cls.reseller["id"], "test")
        principal = authenticate_credentials(cls.key_id, cls.secret, "test")
        cls.context = ApiRequestContext(
            "c" * 32, principal.environment, principal.reseller_id,
            principal.reseller_public_id, principal.api_key_id,
            principal.api_key_public_id,
        )
        db.cur.execute(
            "INSERT INTO categories(name,icon,sort_order,active) VALUES('Subscriptions','',1,1)"
        )
        category_id = int(db.cur.lastrowid)
        product_rows = []
        for i in range(1, cls.PRODUCT_COUNT + 1):
            product_rows.append((
                public_id(i), f"Catalog Product {i:05d}", 10.00 + (i % 20),
                5, "secret", 1, f"Description for product {i}", "1 year",
                "30 days", 5, category_id, 1 if i % 17 == 0 else 0,
            ))
        db.cur.executemany(
            "INSERT INTO products(public_id,name,price,stock,delivery,active,"
            "description,validity,warranty,max_per_order,category_id,featured) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            product_rows,
        )
        db.conn.commit()
        db.cur.execute("SELECT id FROM products ORDER BY id")
        product_ids = [int(row[0]) for row in db.cur.fetchall()]
        db.cur.executemany(
            "INSERT INTO reseller_product_access(reseller_id,product_id,enabled) VALUES(?,?,1)",
            [(cls.reseller["id"], pid) for pid in product_ids],
        )
        db.cur.executemany(
            "INSERT INTO reseller_pricing_rules(reseller_id,product_id,rule_type,"
            "value_cents,value_bps,enabled) VALUES(?,?,'discount_percent',NULL,1000,1)",
            [(cls.reseller["id"], pid) for pid in product_ids[::10]],
        )
        stock_rows = []
        for pid in product_ids[:1000]:
            stock_rows.extend((pid, f"code-{pid}-{n}", 0) for n in range(3))
        db.cur.executemany(
            "INSERT INTO product_stock(product_id,delivery,sold) VALUES(?,?,?)",
            stock_rows,
        )
        db.conn.commit()
        cls.detail_id = public_id(10)

    @classmethod
    def tearDownClass(cls):
        try:
            db.close_thread_connection()
        finally:
            try: os.unlink(_db_path)
            except FileNotFoundError: pass

    def traced(self, fn):
        statements = []
        conn = db.get_conn()
        conn.set_trace_callback(statements.append)
        try:
            started = time.perf_counter()
            result = fn()
            elapsed = time.perf_counter() - started
        finally:
            conn.set_trace_callback(None)
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        return result, selects, elapsed

    def test_paginated_list_uses_two_product_queries_not_n_plus_one(self):
        filters = parse_product_filters({"page": "50", "limit": "25"})
        (products, meta), selects, elapsed = self.traced(
            lambda: get_products(self.context, filters)
        )
        self.assertEqual(len(products), 25)
        self.assertEqual(meta["total"], self.PRODUCT_COUNT)
        self.assertEqual(len(selects), 2, selects)
        self.assertIn("LIMIT 25 OFFSET 1225", selects[1])
        self.assertLess(elapsed, 5.0)

    def test_max_page_response_is_bounded(self):
        products, meta = get_products(
            self.context, parse_product_filters({"page": "1", "limit": "100"})
        )
        self.assertEqual(len(products), 100)
        self.assertEqual(meta["limit"], 100)
        encoded = json.dumps({"products": products, "meta": meta}).encode("utf-8")
        self.assertLess(len(encoded), 100_000)

    def test_detail_has_no_repeated_pricing_queries(self):
        (product, selects, elapsed) = self.traced(
            lambda: get_product(self.context, self.detail_id, "test")
        )
        self.assertEqual(product["id"], self.detail_id)
        self.assertEqual(len(selects), 3, selects)
        self.assertFalse(any("SELECT price FROM products" in s for s in selects))
        self.assertFalse(any("FROM reseller_pricing_rules WHERE" in s for s in selects))
        self.assertLess(elapsed, 2.0)

    def test_category_and_search_remain_paginated(self):
        products, meta = get_products(
            self.context,
            parse_product_filters({
                "page": "1", "limit": "20", "category": "subscriptions",
                "search": "product 01",
            }),
        )
        self.assertLessEqual(len(products), 20)
        self.assertGreater(meta["total"], 0)

    def test_existing_indexes_cover_access_public_id_and_stock_lookups(self):
        expected = {
            "idx_products_public_id", "idx_products_active_name",
            "idx_products_category", "idx_stock_product_sold",
        }
        found = set()
        for table in ("products", "product_stock"):
            db.cur.execute(f"PRAGMA index_list({table})")
            found.update(str(row[1]) for row in db.cur.fetchall())
        self.assertTrue(expected.issubset(found), (expected, found))


if __name__ == "__main__":
    unittest.main(verbosity=2)
