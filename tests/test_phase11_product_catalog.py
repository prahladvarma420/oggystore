"""Phase 11 Prompt 2 — Reseller Product Catalog API tests.

Covers:
  - product list endpoint (service layer)
  - product detail endpoint (service layer)
  - pagination
  - filtering: search, category, availability
  - inactive / disabled product visibility
  - out-of-stock product handling
  - invalid product ID rejection
  - unauthorized / missing-context rejection
  - reseller isolation (Reseller A cannot see Reseller B's products)
  - response field validation (status field, no internal fields)
  - security: IDOR, forbidden parameter rejection
  - access revocation
  - pricing rule applied to response price
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── Isolated temporary database ────────────────────────────────────────────
_fd, _DB = tempfile.mkstemp(prefix="oggy_p11_catalog_", suffix=".db")
os.close(_fd)

import config
config.DB_PATH = _DB
import database.db as db
db.DB_PATH = _DB
db._tls = threading.local()
db.init_db()

import database.reseller_db as rdb
from reseller_api.context import ApiRequestContext
from reseller_api.services.authentication import authenticate_credentials
from reseller_api.services.product_catalog import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    ProductCatalogError,
    _VALID_AVAILABILITY,
    get_product,
    get_products,
    parse_product_detail_request,
    parse_product_filters,
)


# ── Helpers ─────────────────────────────────────────────────────────────────
_counter = 0


def _uid():
    global _counter
    _counter += 1
    return 9_000_000 + _counter


def _make_reseller(name=None):
    uid = _uid()
    suffix = _counter
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, name or f"R{suffix}", f"r_{suffix}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(name or f"Reseller {suffix}", uid, f"user_{suffix}")
    rdb.set_reseller_status(rs["id"], "active")
    return rs


def _make_key(reseller_id, env="test"):
    kid, secret = rdb.create_api_key(reseller_id, env)
    return kid, secret


def _context(kid, secret, env="test"):
    principal = authenticate_credentials(kid, secret, env)
    assert principal is not None, "authentication must succeed"
    return ApiRequestContext(
        request_id="a" * 32,
        environment=principal.environment,
        reseller_id=principal.reseller_id,
        reseller_public_id=principal.reseller_public_id,
        api_key_id=principal.api_key_id,
        api_key_public_id=principal.api_key_public_id,
    )


def _make_product(name=None, price=10.0, active=1, stock_codes=None):
    """Insert a master product and optionally add stock codes."""
    suffix = _counter + 1
    pname = name or f"Product {suffix}"
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active, description, "
        "validity, warranty, rules, redeem_link, max_per_order) "
        "VALUES(?,?,0,'code_stock',?,?,?,?,?,?,?)",
        (pname, price, active,
         f"Description of {pname}", "30 days", "1 year",
         "No rules", "Not required", 1),
    )
    db.conn.commit()
    pid = db.cur.lastrowid
    pub_id = rdb.ensure_product_public_id(pid)
    if stock_codes:
        for code in stock_codes:
            db.cur.execute(
                "INSERT INTO product_stock(product_id, delivery, sold) VALUES(?,?,0)",
                (pid, code),
            )
        db.conn.commit()
    return pid, pub_id


def _grant(reseller_id, product_id):
    rdb.set_product_access(reseller_id, product_id, enabled=True)


def _filters(**kw):
    return parse_product_filters({k: str(v) for k, v in kw.items() if v is not None})


# ── Tests ───────────────────────────────────────────────────────────────────

class TestResponseFields(unittest.TestCase):
    """Product response must include required safe fields, no internal ones."""

    def setUp(self):
        self.rs = _make_reseller()
        self.kid, self.secret = _make_key(self.rs["id"])
        self.ctx = _context(self.kid, self.secret)
        self.pid, self.pub_id = _make_product("Alpha Widget", price=19.99,
                                               stock_codes=["CODE-1"])
        _grant(self.rs["id"], self.pid)

    def _get_single(self):
        filters = _filters()
        products, _ = get_products(self.ctx, filters)
        self.assertEqual(len(products), 1)
        return products[0]

    def test_required_fields_present(self):
        p = self._get_single()
        for field in ("id", "name", "description", "price", "currency",
                      "availability", "status", "max_per_order",
                      "validity", "warranty", "featured"):
            self.assertIn(field, p, f"Missing field: {field}")

    def test_status_is_active(self):
        p = self._get_single()
        self.assertEqual(p["status"], "active")

    def test_currency_is_usdt(self):
        p = self._get_single()
        self.assertEqual(p["currency"], "USDT")

    def test_price_is_string_decimal(self):
        p = self._get_single()
        # from_cents returns a string like '19.99'
        self.assertIsInstance(p["price"], str)
        self.assertIn(".", p["price"])

    def test_id_is_public_id_format(self):
        p = self._get_single()
        import re
        self.assertRegex(p["id"], r"^PRD-[A-HJ-NP-Z2-9]{12}$")

    def test_no_internal_fields_exposed(self):
        p = self._get_single()
        forbidden = {"product_id", "master_price", "rule_type", "value_cents",
                     "value_bps", "fallback_stock", "cost", "delivery",
                     "flash_price", "fulfillment_type", "fulfillment_config",
                     "image_file_id", "redeem_link", "rules"}
        for f in forbidden:
            self.assertNotIn(f, p, f"Internal field exposed: {f}")

    def test_availability_in_stock(self):
        p = self._get_single()
        self.assertEqual(p["availability"], "in_stock")
        self.assertGreater(p["available_quantity"], 0)

    def test_detail_endpoint_same_fields(self):
        product = get_product(self.ctx, self.pub_id, "test")
        self.assertEqual(product["status"], "active")
        self.assertEqual(product["currency"], "USDT")
        self.assertNotIn("cost", product)
        self.assertNotIn("delivery", product)


class TestProductList(unittest.TestCase):
    """GET /api/v1/products list behaviour."""

    def setUp(self):
        self.rs = _make_reseller()
        self.kid, self.secret = _make_key(self.rs["id"])
        self.ctx = _context(self.kid, self.secret)
        # Two products granted, one not granted
        self.p1, self.pub1 = _make_product("ListProd A", stock_codes=["c1"])
        self.p2, self.pub2 = _make_product("ListProd B", stock_codes=["c2"])
        self.p3, self.pub3 = _make_product("ListProd C", stock_codes=["c3"])
        _grant(self.rs["id"], self.p1)
        _grant(self.rs["id"], self.p2)
        # p3 NOT granted → invisible

    def test_returns_only_granted_products(self):
        products, meta = get_products(self.ctx, _filters())
        ids = {p["id"] for p in products}
        self.assertIn(self.pub1, ids)
        self.assertIn(self.pub2, ids)
        self.assertNotIn(self.pub3, ids)

    def test_pagination_meta_fields(self):
        _, meta = get_products(self.ctx, _filters())
        for key in ("page", "limit", "total", "pages", "has_next", "has_previous"):
            self.assertIn(key, meta, f"Missing pagination key: {key}")

    def test_default_pagination_values(self):
        _, meta = get_products(self.ctx, _filters())
        self.assertEqual(meta["page"], 1)
        self.assertEqual(meta["limit"], DEFAULT_LIMIT)

    def test_total_counts_only_accessible_products(self):
        _, meta = get_products(self.ctx, _filters())
        self.assertEqual(meta["total"], 2)

    def test_empty_list_for_new_reseller(self):
        rs2 = _make_reseller()
        kid2, sec2 = _make_key(rs2["id"])
        ctx2 = _context(kid2, sec2)
        products, meta = get_products(ctx2, _filters())
        self.assertEqual(products, [])
        self.assertEqual(meta["total"], 0)


class TestPagination(unittest.TestCase):
    """Pagination correctness."""

    def setUp(self):
        self.rs = _make_reseller()
        self.kid, self.secret = _make_key(self.rs["id"])
        self.ctx = _context(self.kid, self.secret)
        # Create 5 products, all granted
        self.pids = []
        for i in range(5):
            pid, _ = _make_product(f"Page Prod {i}")
            _grant(self.rs["id"], pid)
            self.pids.append(pid)

    def test_limit_respected(self):
        products, _ = get_products(self.ctx, _filters(limit=2))
        self.assertEqual(len(products), 2)

    def test_page_2_returns_different_products(self):
        p1, _ = get_products(self.ctx, _filters(limit=2, page=1))
        p2, _ = get_products(self.ctx, _filters(limit=2, page=2))
        ids1 = {p["id"] for p in p1}
        ids2 = {p["id"] for p in p2}
        self.assertTrue(ids1.isdisjoint(ids2),
                        "Page 1 and page 2 must not return the same products")

    def test_has_next_true_when_more_pages(self):
        _, meta = get_products(self.ctx, _filters(limit=2, page=1))
        self.assertTrue(meta["has_next"])

    def test_has_previous_false_on_page_1(self):
        _, meta = get_products(self.ctx, _filters(limit=2, page=1))
        self.assertFalse(meta["has_previous"])

    def test_has_previous_true_on_page_2(self):
        _, meta = get_products(self.ctx, _filters(limit=2, page=2))
        self.assertTrue(meta["has_previous"])

    def test_page_beyond_last_returns_empty(self):
        products, _ = get_products(self.ctx, _filters(limit=5, page=999))
        self.assertEqual(products, [])

    def test_limit_max_enforced(self):
        with self.assertRaises(ProductCatalogError) as cm:
            _filters(limit=MAX_LIMIT + 1)
        self.assertIn("invalid_pagination", str(cm.exception))

    def test_page_zero_rejected(self):
        with self.assertRaises(ProductCatalogError):
            _filters(page=0)


class TestFiltering(unittest.TestCase):
    """Search, category, and availability filters."""

    def setUp(self):
        self.rs = _make_reseller()
        self.kid, self.secret = _make_key(self.rs["id"])
        self.ctx = _context(self.kid, self.secret)

        # Category
        db.cur.execute(
            "INSERT INTO categories(name, icon, sort_order, active) VALUES(?,?,?,1)",
            ("Electronics", "💡", 1),
        )
        db.conn.commit()
        self.cat_id = db.cur.lastrowid

        # Products
        self.pa, self.puba = _make_product("Searchable Gadget", stock_codes=["ga1"])
        db.cur.execute("UPDATE products SET category_id=? WHERE id=?",
                       (self.cat_id, self.pa))
        db.conn.commit()
        _grant(self.rs["id"], self.pa)

        self.pb, self.pubb = _make_product("Unrelated Thing")  # no stock → out_of_stock
        _grant(self.rs["id"], self.pb)

    def test_search_by_name(self):
        products, meta = get_products(self.ctx, _filters(search="Gadget"))
        ids = {p["id"] for p in products}
        self.assertIn(self.puba, ids)
        self.assertNotIn(self.pubb, ids)

    def test_search_case_insensitive(self):
        products, _ = get_products(self.ctx, _filters(search="gadget"))
        self.assertEqual(len(products), 1)

    def test_search_no_results(self):
        products, meta = get_products(self.ctx, _filters(search="zzz_no_match"))
        self.assertEqual(products, [])
        self.assertEqual(meta["total"], 0)

    def test_category_filter(self):
        products, _ = get_products(self.ctx, _filters(category="Electronics"))
        ids = {p["id"] for p in products}
        self.assertIn(self.puba, ids)
        self.assertNotIn(self.pubb, ids)

    def test_category_case_insensitive(self):
        products, _ = get_products(self.ctx, _filters(category="electronics"))
        self.assertEqual(len(products), 1)

    def test_category_uncategorized(self):
        products, _ = get_products(self.ctx, _filters(category="uncategorized"))
        # pb has no category_id → should appear
        ids = {p["id"] for p in products}
        self.assertIn(self.pubb, ids)
        self.assertNotIn(self.puba, ids)

    def test_availability_filter_in_stock(self):
        products, _ = get_products(self.ctx, _filters(availability="in_stock"))
        ids = {p["id"] for p in products}
        self.assertIn(self.puba, ids)   # has stock code
        self.assertNotIn(self.pubb, ids)  # no stock

    def test_availability_filter_out_of_stock(self):
        products, _ = get_products(self.ctx, _filters(availability="out_of_stock"))
        ids = {p["id"] for p in products}
        self.assertNotIn(self.puba, ids)
        self.assertIn(self.pubb, ids)

    def test_availability_filter_invalid_rejected(self):
        with self.assertRaises(ProductCatalogError) as cm:
            _filters(availability="unknown_value")
        self.assertIn("invalid_filter", str(cm.exception))

    def test_valid_availability_values(self):
        self.assertEqual(_VALID_AVAILABILITY, {"in_stock", "out_of_stock"})

    def test_search_too_long_rejected(self):
        with self.assertRaises(ProductCatalogError) as cm:
            _filters(search="x" * 200)
        self.assertIn("invalid_filter", str(cm.exception))

    def test_duplicate_param_rejected(self):
        class FakeArgs:
            def keys(self): return ["page", "page"]
            def get(self, k, d=None):
                return {"page": "1"}.get(k, d)
            def getlist(self, k):
                return ["1", "2"] if k == "page" else []
        with self.assertRaises(ProductCatalogError):
            parse_product_filters(FakeArgs())


class TestInactiveAndHiddenProducts(unittest.TestCase):
    """Inactive/disabled products must never appear in reseller catalog."""

    def setUp(self):
        self.rs = _make_reseller()
        self.kid, self.secret = _make_key(self.rs["id"])
        self.ctx = _context(self.kid, self.secret)

        # Active product → visible
        self.active_pid, self.active_pub = _make_product("Active Product",
                                                          stock_codes=["ac1"])
        _grant(self.rs["id"], self.active_pid)

        # Inactive product (active=0) → must NOT appear even with access row
        self.inactive_pid, self.inactive_pub = _make_product("Inactive Product",
                                                               active=0,
                                                               stock_codes=["in1"])
        _grant(self.rs["id"], self.inactive_pid)

        # Active product, no access row → must NOT appear
        self.noaccess_pid, self.noaccess_pub = _make_product("NoAccess Product",
                                                              stock_codes=["na1"])
        # no _grant call

    def test_inactive_product_not_in_list(self):
        products, _ = get_products(self.ctx, _filters())
        ids = {p["id"] for p in products}
        self.assertIn(self.active_pub, ids)
        self.assertNotIn(self.inactive_pub, ids)

    def test_inactive_product_not_in_detail(self):
        with self.assertRaises(ProductCatalogError) as cm:
            get_product(self.ctx, self.inactive_pub, "test")
        self.assertEqual(cm.exception.status, 404)

    def test_product_without_access_not_in_list(self):
        products, _ = get_products(self.ctx, _filters())
        ids = {p["id"] for p in products}
        self.assertNotIn(self.noaccess_pub, ids)

    def test_product_without_access_detail_is_404(self):
        with self.assertRaises(ProductCatalogError) as cm:
            get_product(self.ctx, self.noaccess_pub, "test")
        self.assertEqual(cm.exception.status, 404)

    def test_revoked_access_hides_product(self):
        """Re-disabling access must immediately hide the product."""
        rdb.set_product_access(self.rs["id"], self.active_pid, enabled=False)
        products, _ = get_products(self.ctx, _filters())
        ids = {p["id"] for p in products}
        self.assertNotIn(self.active_pub, ids)
        # Restore for teardown isolation
        rdb.set_product_access(self.rs["id"], self.active_pid, enabled=True)


class TestOutOfStock(unittest.TestCase):
    """Out-of-stock products appear with correct availability, no code leakage."""

    def setUp(self):
        self.rs = _make_reseller()
        self.kid, self.secret = _make_key(self.rs["id"])
        self.ctx = _context(self.kid, self.secret)

        self.oos_pid, self.oos_pub = _make_product("Out Of Stock Item")
        # No stock codes → available_quantity = 0
        _grant(self.rs["id"], self.oos_pid)

        self.stocked_pid, self.stocked_pub = _make_product(
            "Stocked Item", stock_codes=["code_abc"])
        _grant(self.rs["id"], self.stocked_pid)

    def test_out_of_stock_still_visible_in_list(self):
        products, _ = get_products(self.ctx, _filters())
        ids = {p["id"] for p in products}
        self.assertIn(self.oos_pub, ids)

    def test_out_of_stock_availability_value(self):
        products, _ = get_products(self.ctx, _filters())
        oos = next(p for p in products if p["id"] == self.oos_pub)
        self.assertEqual(oos["availability"], "out_of_stock")
        self.assertEqual(oos["available_quantity"], 0)

    def test_stocked_availability_value(self):
        products, _ = get_products(self.ctx, _filters())
        stocked = next(p for p in products if p["id"] == self.stocked_pub)
        self.assertEqual(stocked["availability"], "in_stock")
        self.assertGreater(stocked["available_quantity"], 0)

    def test_stock_codes_never_in_response(self):
        products, _ = get_products(self.ctx, _filters())
        for p in products:
            self.assertNotIn("delivery", p)
            for v in p.values():
                if isinstance(v, str):
                    self.assertNotIn("code_abc", v,
                                     "Raw stock code must not leak into API response")

    def test_out_of_stock_detail_has_no_codes(self):
        product = get_product(self.ctx, self.oos_pub, "test")
        self.assertNotIn("delivery", product)
        self.assertEqual(product["availability"], "out_of_stock")


class TestProductDetail(unittest.TestCase):
    """GET /api/v1/products/{product_id} behaviour."""

    def setUp(self):
        self.rs = _make_reseller()
        self.kid, self.secret = _make_key(self.rs["id"])
        self.ctx = _context(self.kid, self.secret)
        self.pid, self.pub_id = _make_product("Detail Widget", price=29.50,
                                               stock_codes=["dw1", "dw2"])
        _grant(self.rs["id"], self.pid)

    def test_valid_public_id_returns_product(self):
        product = get_product(self.ctx, self.pub_id, "test")
        self.assertEqual(product["id"], self.pub_id)
        self.assertEqual(product["name"], "Detail Widget")

    def test_invalid_format_raises_404(self):
        """Malformed public ID must raise a 404-equivalent error."""
        with self.assertRaises(ProductCatalogError) as cm:
            parse_product_detail_request("not-a-valid-id")
        self.assertEqual(cm.exception.status, 404)

    def test_wrong_id_raises_404(self):
        """A valid-format ID that doesn't exist must return 404."""
        fake_id = "PRD-AAAAAAAAAAAA"
        with self.assertRaises(ProductCatalogError) as cm:
            get_product(self.ctx, fake_id, "test")
        self.assertEqual(cm.exception.status, 404)

    def test_integer_id_rejected(self):
        with self.assertRaises(ProductCatalogError):
            parse_product_detail_request(str(self.pid))

    def test_empty_id_rejected(self):
        with self.assertRaises(ProductCatalogError):
            parse_product_detail_request("")

    def test_query_params_on_detail_rejected(self):
        with self.assertRaises(ProductCatalogError):
            parse_product_detail_request(self.pub_id, {"extra": "param"})

    def test_reseller_identity_param_on_detail_rejected(self):
        with self.assertRaises(ProductCatalogError):
            parse_product_detail_request(self.pub_id, {"reseller_id": "1"})


class TestResellerIsolation(unittest.TestCase):
    """Reseller A must not see Reseller B's products (IDOR prevention)."""

    def setUp(self):
        self.rsA = _make_reseller("Reseller A")
        self.rsB = _make_reseller("Reseller B")

        kidA, secA = _make_key(self.rsA["id"])
        kidB, secB = _make_key(self.rsB["id"])
        self.ctxA = _context(kidA, secA)
        self.ctxB = _context(kidB, secB)

        # Product granted only to B
        self.pB, self.pubB = _make_product("B-Only Product", stock_codes=["b1"])
        _grant(self.rsB["id"], self.pB)

        # Product granted only to A
        self.pA, self.pubA = _make_product("A-Only Product", stock_codes=["a1"])
        _grant(self.rsA["id"], self.pA)

    def test_reseller_a_cannot_see_b_product_in_list(self):
        products, _ = get_products(self.ctxA, _filters())
        ids = {p["id"] for p in products}
        self.assertNotIn(self.pubB, ids)

    def test_reseller_b_cannot_see_a_product_in_list(self):
        products, _ = get_products(self.ctxB, _filters())
        ids = {p["id"] for p in products}
        self.assertNotIn(self.pubA, ids)

    def test_reseller_a_cannot_fetch_b_product_detail(self):
        """A must get 404 when fetching B's product_id, not a permission error."""
        with self.assertRaises(ProductCatalogError) as cm:
            get_product(self.ctxA, self.pubB, "test")
        self.assertEqual(cm.exception.status, 404,
                         "Must return 404, not 403, to prevent existence oracle")

    def test_reseller_b_cannot_fetch_a_product_detail(self):
        with self.assertRaises(ProductCatalogError) as cm:
            get_product(self.ctxB, self.pubA, "test")
        self.assertEqual(cm.exception.status, 404)

    def test_error_is_identical_for_nonexistent_vs_unauthorized(self):
        """product_not_found code must be the same for both cases (no oracle)."""
        with self.assertRaises(ProductCatalogError) as cm_cross:
            get_product(self.ctxA, self.pubB, "test")
        with self.assertRaises(ProductCatalogError) as cm_nonexist:
            get_product(self.ctxA, "PRD-ZZZZZZZZZZZZ", "test")
        self.assertEqual(cm_cross.exception.code, cm_nonexist.exception.code)
        self.assertEqual(cm_cross.exception.status, cm_nonexist.exception.status)


class TestUnauthorizedAccess(unittest.TestCase):
    """Requests without a valid context must be rejected."""

    def setUp(self):
        self.rs = _make_reseller()
        self.kid, self.secret = _make_key(self.rs["id"])
        self.ctx = _context(self.kid, self.secret)
        self.pid, self.pub_id = _make_product("Auth Widget", stock_codes=["aw1"])
        _grant(self.rs["id"], self.pid)

    def test_none_context_list_raises(self):
        with self.assertRaises(ProductCatalogError) as cm:
            get_products(None, _filters())
        self.assertIn("authentication_failed", str(cm.exception))

    def test_none_context_detail_raises(self):
        with self.assertRaises(ProductCatalogError) as cm:
            get_product(None, self.pub_id, "test")
        self.assertIn("authentication_failed", str(cm.exception))

    def test_wrong_environment_key_rejected(self):
        """A production key must not authenticate in the test environment."""
        prod_kid, prod_secret = rdb.create_api_key(self.rs["id"], "production")
        principal = authenticate_credentials(prod_kid, prod_secret, "test")
        self.assertIsNone(principal,
                          "Production key must not authenticate against test env")

    def test_revoked_key_rejected(self):
        kid2, sec2 = rdb.create_api_key(self.rs["id"], "test")
        rdb.revoke_api_key(kid2)
        principal = authenticate_credentials(kid2, sec2, "test")
        self.assertIsNone(principal)

    def test_suspended_reseller_rejected(self):
        rs2 = _make_reseller("Suspended")
        kid2, sec2 = rdb.create_api_key(rs2["id"], "test")
        rdb.set_reseller_status(rs2["id"], "suspended")
        principal = authenticate_credentials(kid2, sec2, "test")
        self.assertIsNone(principal)


class TestForbiddenParameters(unittest.TestCase):
    """Forbidden / identity / unsupported query parameters must be rejected."""

    def test_reseller_id_param_rejected(self):
        with self.assertRaises(ProductCatalogError) as cm:
            parse_product_filters({"reseller_id": "1"})
        self.assertIn("invalid_parameter", str(cm.exception))

    def test_reseller_public_id_param_rejected(self):
        with self.assertRaises(ProductCatalogError) as cm:
            parse_product_filters({"reseller_public_id": "abc"})
        self.assertIn("invalid_parameter", str(cm.exception))

    def test_status_param_rejected(self):
        with self.assertRaises(ProductCatalogError) as cm:
            parse_product_filters({"status": "active"})
        self.assertIn("invalid_filter", str(cm.exception))

    def test_price_param_rejected(self):
        with self.assertRaises(ProductCatalogError) as cm:
            parse_product_filters({"price": "10"})
        self.assertIn("invalid_filter", str(cm.exception))

    def test_unknown_param_rejected(self):
        with self.assertRaises(ProductCatalogError) as cm:
            parse_product_filters({"sortby": "name"})
        self.assertIn("invalid_parameter", str(cm.exception))

    def test_cost_param_on_detail_rejected(self):
        pid, pub_id = _make_product("FP Widget")
        with self.assertRaises(ProductCatalogError):
            parse_product_detail_request(pub_id, {"cost": "0"})


class TestPricingRuleApplied(unittest.TestCase):
    """Reseller pricing rules must be reflected in the API response price."""

    def setUp(self):
        self.rs = _make_reseller()
        self.kid, self.secret = _make_key(self.rs["id"])
        self.ctx = _context(self.kid, self.secret)
        # Master price = $20.00
        self.pid, self.pub_id = _make_product("Priced Product", price=20.0,
                                               stock_codes=["pp1"])
        _grant(self.rs["id"], self.pid)

    def test_master_price_returned_when_no_rule(self):
        products, _ = get_products(self.ctx, _filters())
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["price"], "20.00")

    def test_fixed_price_rule_applied(self):
        rdb.set_pricing_rule(self.rs["id"], self.pid, "fixed_price", 1500)  # $15.00
        products, _ = get_products(self.ctx, _filters())
        self.assertEqual(products[0]["price"], "15.00")

    def test_discount_percent_rule_applied(self):
        # 10% discount on $20.00 → $18.00
        rdb.set_pricing_rule(self.rs["id"], self.pid, "discount_percent", 1000)  # 10%
        products, _ = get_products(self.ctx, _filters())
        self.assertEqual(products[0]["price"], "18.00")

    def test_markup_percent_rule_applied(self):
        # 25% markup on $20.00 → $25.00
        rdb.set_pricing_rule(self.rs["id"], self.pid, "markup_percent", 2500)  # 25%
        products, _ = get_products(self.ctx, _filters())
        self.assertEqual(products[0]["price"], "25.00")

    def test_deleted_rule_reverts_to_master(self):
        rdb.set_pricing_rule(self.rs["id"], self.pid, "fixed_price", 500)
        rdb.delete_pricing_rule(self.rs["id"], self.pid)
        products, _ = get_products(self.ctx, _filters())
        self.assertEqual(products[0]["price"], "20.00")

    def test_price_never_exposed_as_raw_float(self):
        """price field must be a decimal string, never a raw float."""
        products, _ = get_products(self.ctx, _filters())
        self.assertNotIsInstance(products[0]["price"], float)


class TestParseProductFilters(unittest.TestCase):
    """parse_product_filters return structure validation."""

    def test_returns_all_keys(self):
        f = parse_product_filters({})
        for key in ("page", "limit", "search", "category", "availability"):
            self.assertIn(key, f)

    def test_defaults(self):
        f = parse_product_filters({})
        self.assertEqual(f["page"], 1)
        self.assertEqual(f["limit"], DEFAULT_LIMIT)
        self.assertEqual(f["search"], "")
        self.assertEqual(f["category"], "")
        self.assertEqual(f["availability"], "")

    def test_empty_availability_is_no_filter(self):
        f = parse_product_filters({"availability": ""})
        self.assertEqual(f["availability"], "")

    def test_in_stock_accepted(self):
        f = parse_product_filters({"availability": "in_stock"})
        self.assertEqual(f["availability"], "in_stock")

    def test_out_of_stock_accepted(self):
        f = parse_product_filters({"availability": "out_of_stock"})
        self.assertEqual(f["availability"], "out_of_stock")


# ── Cleanup ─────────────────────────────────────────────────────────────────
class _Cleanup(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        try:
            db.close_thread_connection()
        except Exception:
            pass
        try:
            os.unlink(_DB)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    unittest.main()
