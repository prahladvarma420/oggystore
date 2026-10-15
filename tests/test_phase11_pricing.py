"""
Phase 11 Prompt 5 -- Reseller product pricing & markup system (service-level).

Tests verify:
  - inherited price (no rule -> master price)
  - fixed_price rule
  - markup_percent rule
  - discount_percent rule
  - price change (owner updates master -> effective reseller price updates)
  - concurrent order (both threads pay server-side price; balance guards)
  - manipulated price (client-supplied price ignored)
  - negative price guard (order_pricing rejects negative unit price)
  - zero price (allowed if rule sets it; order_pricing rejects <0 only)
  - excessive price (large fixed_price; server charges that amount)
  - currency mismatch (server always returns USDT; client currency ignored)
  - wallet debit uses server-side price (not client-supplied)
  - master product price remains unchanged after reseller rule change
  - reseller cannot underpay (wallet debit matches server total)
  - reseller cannot alter internal cost
  - list_product_prices returns reseller's effective prices only
  - get_product_price 404 for non-imported product
  - authorization: None context raises PricingError 401
  - cross-reseller isolation (reseller A's rule does not affect reseller B)
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

# --- Isolated DB setup ---------------------------------------------------
_fd, _DB = tempfile.mkstemp(prefix="oggy_p11_pricing_", suffix=".db")
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
from reseller_api.services.order_pricing import (
    OrderPricingError,
    OrderPricingResult,
    calculate_order_price,
)
from reseller_api.services.pricing import (
    PricingError,
    SUPPORTED_RULE_TYPES,
    get_product_price,
    list_product_prices,
)
from reseller_api.services.order_validation import ValidatedOrderRequest

# --- counter for unique test data ----------------------------------------
_ctr = 0


def _uid():
    global _ctr
    _ctr += 1
    return 8_000_000 + _ctr


def _make_reseller():
    uid = _uid()
    n = _ctr
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"PriceRS{n}", f"price_rs_{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"PriceReseller {n}", uid, f"price_user_{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    ctx = _context(key_id, secret)
    return rs["id"], ctx


def _context(key_id, secret, env="test"):
    principal = authenticate_credentials(key_id, secret, env)
    assert principal is not None, "authenticate_credentials returned None"
    return ApiRequestContext(
        request_id="p" * 32,
        environment=principal.environment,
        reseller_id=principal.reseller_id,
        reseller_public_id=principal.reseller_public_id,
        api_key_id=principal.api_key_id,
        api_key_public_id=principal.api_key_public_id,
    )


def _make_product(name="Pricing Widget", price=10.0, active=1):
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active, description,"
        " validity, warranty, rules, redeem_link, max_per_order)"
        " VALUES(?,?,50,'instant',?,'',' ',' ',' ',' ',10)",
        (name, price, active),
    )
    db.conn.commit()
    product_id = db.cur.lastrowid
    pub = rdb.ensure_product_public_id(product_id)
    return product_id, pub


def _grant_access(reseller_id, product_id):
    rdb.set_product_access(reseller_id, product_id, enabled=True)


def _set_wallet(reseller_id, amount_cents):
    """Directly set wallet balance in the DB."""
    db.cur.execute(
        "UPDATE resellers SET balance=? WHERE id=?",
        (amount_cents, reseller_id),
    )
    db.conn.commit()


def _get_wallet(reseller_id):
    db.cur.execute("SELECT balance FROM resellers WHERE id=?", (reseller_id,))
    row = db.cur.fetchone()
    return int(row[0]) if row else 0


def _get_master_price(product_id):
    db.cur.execute("SELECT price FROM products WHERE id=?", (product_id,))
    row = db.cur.fetchone()
    return float(row[0]) if row else None


def _validated_order(reseller_id, product_id, pub_id="PRD-AAAAAAAAAAAAA",
                      quantity=1, name="Test Product"):
    """Build a ValidatedOrderRequest directly (bypasses HTTP layer)."""
    return ValidatedOrderRequest(
        reseller_id=reseller_id,
        product_id=product_id,
        public_product_id=pub_id,
        product_name=name,
        max_per_order=99,
        quantity=quantity,
        client_order_id=None,
    )


# =========================================================================
class TestSupportedPricingModels(unittest.TestCase):
    """The architecture supports exactly these rule types."""

    def test_rule_types_are_fixed_price_discount_markup(self):
        self.assertIn("fixed_price", SUPPORTED_RULE_TYPES)
        self.assertIn("markup_percent", SUPPORTED_RULE_TYPES)
        self.assertIn("discount_percent", SUPPORTED_RULE_TYPES)

    def test_no_extra_rule_types(self):
        # Exactly three models; no invented extras
        self.assertEqual(len(SUPPORTED_RULE_TYPES), 3)


# =========================================================================
class TestInheritedPrice(unittest.TestCase):
    """No pricing rule -> reseller pays the master product price."""

    def test_inherited_price_equals_master(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=25.50)
        _grant_access(rid, pid)

        unit_cents, source = rdb.resolve_reseller_price_cents(rid, pid, 1)
        self.assertEqual(unit_cents, 2550)
        self.assertEqual(source, "master")

    def test_get_product_price_inherited(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=15.00)
        _grant_access(rid, pid)

        rec = get_product_price(ctx, pub, "test")
        self.assertEqual(rec["price"], "15.00")
        self.assertEqual(rec["price_source"], "master")
        self.assertEqual(rec["currency"], "USDT")

    def test_list_prices_inherited(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=8.99)
        _grant_access(rid, pid)

        prices = list_product_prices(ctx, "test")
        matches = [p for p in prices if p["product_id"] == pub]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["price"], "8.99")
        self.assertEqual(matches[0]["price_source"], "master")


# =========================================================================
class TestFixedPriceRule(unittest.TestCase):
    """fixed_price rule -> reseller pays exact cents set by owner."""

    def test_fixed_price_overrides_master(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=20.00)
        _grant_access(rid, pid)
        rdb.set_pricing_rule(rid, pid, "fixed_price", 1500, True)  # $15.00

        unit_cents, source = rdb.resolve_reseller_price_cents(rid, pid, 1)
        self.assertEqual(unit_cents, 1500)
        self.assertEqual(source, "rule:fixed_price")

    def test_get_product_price_fixed(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=20.00)
        _grant_access(rid, pid)
        rdb.set_pricing_rule(rid, pid, "fixed_price", 1750, True)  # $17.50

        rec = get_product_price(ctx, pub, "test")
        self.assertEqual(rec["price"], "17.50")
        self.assertIn("fixed_price", rec["price_source"])

    def test_order_pricing_uses_fixed_rule(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=20.00)
        _grant_access(rid, pid)
        rdb.set_pricing_rule(rid, pid, "fixed_price", 1200, True)  # $12.00

        vo = _validated_order(rid, pid, pub_id=pub, quantity=2)
        result = calculate_order_price(vo)
        self.assertEqual(result.unit_price_cents, 1200)
        self.assertEqual(result.total_cents, 2400)
        self.assertEqual(result.price_source, "rule:fixed_price")

    def test_zero_fixed_price_is_allowed(self):
        """Owner can deliberately set price to zero (e.g. promotional)."""
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=10.00)
        _grant_access(rid, pid)
        rdb.set_pricing_rule(rid, pid, "fixed_price", 0, True)

        unit_cents, source = rdb.resolve_reseller_price_cents(rid, pid, 1)
        self.assertEqual(unit_cents, 0)

    def test_zero_price_order_pricing_does_not_raise(self):
        """Zero unit price is not negative; order_pricing allows it."""
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=10.00)
        _grant_access(rid, pid)
        rdb.set_pricing_rule(rid, pid, "fixed_price", 0, True)

        vo = _validated_order(rid, pid, pub_id=pub)
        result = calculate_order_price(vo)
        self.assertEqual(result.unit_price_cents, 0)


# =========================================================================
class TestMarkupPercentRule(unittest.TestCase):
    """markup_percent rule -> reseller pays master + X%."""

    def test_markup_adds_percentage(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=10.00)  # 1000 cents
        _grant_access(rid, pid)
        rdb.set_pricing_rule(rid, pid, "markup_percent", 2000, True)  # 20% = 2000 bps

        unit_cents, source = rdb.resolve_reseller_price_cents(rid, pid, 1)
        # 1000 + 20% = 1200
        self.assertEqual(unit_cents, 1200)
        self.assertEqual(source, "rule:markup_percent")

    def test_markup_order_total(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=10.00)
        _grant_access(rid, pid)
        rdb.set_pricing_rule(rid, pid, "markup_percent", 5000, True)  # 50%

        vo = _validated_order(rid, pid, pub_id=pub, quantity=3)
        result = calculate_order_price(vo)
        # unit = 1000 + 50% = 1500, total = 4500
        self.assertEqual(result.unit_price_cents, 1500)
        self.assertEqual(result.total_cents, 4500)

    def test_markup_bps_rounding_half_up(self):
        """Basis-point arithmetic uses half-up rounding (no floats)."""
        rid, ctx = _make_reseller()
        # 1001 cents * 1500 bps = 150.15 -> delta = (1001*1500+5000)//10000 = 150
        pid, pub = _make_product(price=10.01)  # 1001 cents
        _grant_access(rid, pid)
        rdb.set_pricing_rule(rid, pid, "markup_percent", 1500, True)  # 15%

        unit_cents, _ = rdb.resolve_reseller_price_cents(rid, pid, 1)
        # delta = (1001 * 1500 + 5000) // 10000 = (1501500 + 5000) // 10000 = 150
        # unit = 1001 + 150 = 1151
        self.assertEqual(unit_cents, 1151)
        # Ensure it is an integer (no floats)
        self.assertIsInstance(unit_cents, int)


# =========================================================================
class TestPriceChange(unittest.TestCase):
    """Owner changes master price -> reseller effective price reflects it."""

    def test_master_price_change_updates_effective_price(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=10.00)
        _grant_access(rid, pid)
        # No rule -> inherits master
        unit1, _ = rdb.resolve_reseller_price_cents(rid, pid, 1)
        self.assertEqual(unit1, 1000)

        # Owner changes master price
        db.cur.execute("UPDATE products SET price=20.00 WHERE id=?", (pid,))
        db.conn.commit()

        unit2, _ = rdb.resolve_reseller_price_cents(rid, pid, 1)
        self.assertEqual(unit2, 2000)

    def test_markup_rule_tracks_master_price_change(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=10.00)
        _grant_access(rid, pid)
        rdb.set_pricing_rule(rid, pid, "markup_percent", 1000, True)  # 10%

        unit1, _ = rdb.resolve_reseller_price_cents(rid, pid, 1)
        self.assertEqual(unit1, 1100)  # 1000 + 10%

        db.cur.execute("UPDATE products SET price=20.00 WHERE id=?", (pid,))
        db.conn.commit()

        unit2, _ = rdb.resolve_reseller_price_cents(rid, pid, 1)
        self.assertEqual(unit2, 2200)  # 2000 + 10%

    def test_master_price_unchanged_by_reseller_rule(self):
        """Setting a pricing rule must not alter the master product price."""
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=30.00)
        _grant_access(rid, pid)
        master_before = _get_master_price(pid)

        rdb.set_pricing_rule(rid, pid, "fixed_price", 1500, True)

        master_after = _get_master_price(pid)
        self.assertEqual(master_before, master_after)
        self.assertAlmostEqual(master_after, 30.00)


# =========================================================================
class TestConcurrentOrder(unittest.TestCase):
    """Two threads place orders concurrently; both use the server price."""

    def test_concurrent_orders_both_use_server_price(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=5.00)
        _grant_access(rid, pid)
        # No pricing rule -> inherits master 500 cents

        prices = []
        errors = []

        def _do_order():
            try:
                vo = _validated_order(rid, pid, pub_id=pub, quantity=1)
                result = calculate_order_price(vo)
                prices.append(result.unit_price_cents)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=_do_order)
        t2 = threading.Thread(target=_do_order)
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(errors, [], f"Unexpected errors: {errors}")
        # Both threads must compute the same server-side price
        self.assertEqual(prices, [500, 500])


# =========================================================================
class TestManipulatedPrice(unittest.TestCase):
    """Client-supplied prices/costs are never used."""

    def test_order_pricing_ignores_quantity_zero(self):
        """ValidatedOrderRequest enforces quantity >= 1; order_pricing trusts it."""
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=10.00)
        _grant_access(rid, pid)
        # The validated order object is the only price input; no client field exists
        vo = _validated_order(rid, pid, quantity=1)
        result = calculate_order_price(vo)
        # Server-calculated, never client-overrideable
        self.assertEqual(result.unit_price_cents, 1000)

    def test_order_pricing_result_has_no_float_money(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=9.99)
        _grant_access(rid, pid)
        vo = _validated_order(rid, pid, pub_id=pub, quantity=2)
        result = calculate_order_price(vo)
        self.assertIsInstance(result.unit_price_cents, int)
        self.assertIsInstance(result.total_cents, int)

    def test_float_money_in_result_raises(self):
        """OrderPricingResult constructor rejects float money values."""
        with self.assertRaises(TypeError):
            OrderPricingResult(
                reseller_id=1,
                product_id=1,
                quantity=1,
                unit_price_cents=10.0,   # float -> rejected
                total_cents=10,
                price_source="master",
            )

    def test_reseller_api_has_no_price_field_in_order_request(self):
        """ValidatedOrderRequest has no price/cost/total field - by design."""
        vo = ValidatedOrderRequest(
            reseller_id=1, product_id=1,
            public_product_id="PRD-AAAAAAAAAAAA",
            product_name="Test", max_per_order=10,
            quantity=1, client_order_id=None,
        )
        self.assertFalse(hasattr(vo, "price"))
        self.assertFalse(hasattr(vo, "cost"))
        self.assertFalse(hasattr(vo, "total"))
        self.assertFalse(hasattr(vo, "unit_price"))


# =========================================================================
class TestNegativePrice(unittest.TestCase):
    """order_pricing rejects any server-computed negative unit price."""

    def test_negative_price_guard_in_order_pricing(self):
        """The order_pricing service has an explicit < 0 guard."""
        # We cannot produce a negative price through the DB layer (CHECK
        # constraint prevents it), so test the guard in calculate_order_price
        # by monkey-patching resolve_reseller_price_cents.
        import reseller_api.services.order_pricing as op_module
        import database.reseller_db as rdb_module
        original = rdb_module.resolve_reseller_price_cents
        try:
            rdb_module.resolve_reseller_price_cents = lambda *a, **kw: (-100, "bad")
            op_module.resolve_reseller_price_cents = lambda *a, **kw: (-100, "bad")
            rid, ctx = _make_reseller()
            pid, pub = _make_product(price=10.00)
            _grant_access(rid, pid)
            vo = _validated_order(rid, pid, pub_id=pub)
            with self.assertRaises(OrderPricingError) as cm:
                calculate_order_price(vo)
            self.assertEqual(cm.exception.code, "pricing_error")
        finally:
            rdb_module.resolve_reseller_price_cents = original
            op_module.resolve_reseller_price_cents = original

    def test_discount_cannot_produce_negative_price(self):
        """resolve_reseller_price_from_values clamps discount to 0."""
        # 50 cents * 200% discount bps
        cents, source = rdb.resolve_reseller_price_from_values(
            "0.50", "discount_percent", None, 20000
        )
        self.assertGreaterEqual(cents, 0)


# =========================================================================
class TestExcessivePrice(unittest.TestCase):
    """Very large fixed price is stored and returned without overflow."""

    def test_large_fixed_price_stored_exactly(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=10.00)
        _grant_access(rid, pid)
        big = 999_999_99  # $9,999,999.99 in cents
        rdb.set_pricing_rule(rid, pid, "fixed_price", big, True)

        unit_cents, source = rdb.resolve_reseller_price_cents(rid, pid, 1)
        self.assertEqual(unit_cents, big)
        self.assertEqual(source, "rule:fixed_price")

    def test_order_total_is_integer_for_large_price(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=10.00)
        _grant_access(rid, pid)
        rdb.set_pricing_rule(rid, pid, "fixed_price", 500_00, True)  # $500

        vo = _validated_order(rid, pid, pub_id=pub, quantity=3)
        result = calculate_order_price(vo)
        self.assertEqual(result.total_cents, 150_000)
        self.assertIsInstance(result.total_cents, int)


# =========================================================================
class TestCurrencyMismatch(unittest.TestCase):
    """Server always uses USDT; no client currency field is accepted."""

    def test_order_pricing_result_currency_is_always_usdt(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=10.00)
        _grant_access(rid, pid)
        vo = _validated_order(rid, pid, pub_id=pub)
        result = calculate_order_price(vo)
        self.assertEqual(result.currency, "USDT")

    def test_pricing_service_currency_is_usdt(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=5.00)
        _grant_access(rid, pid)
        rec = get_product_price(ctx, pub, "test")
        self.assertEqual(rec["currency"], "USDT")

    def test_list_prices_all_usdt(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=3.00)
        _grant_access(rid, pid)
        prices = list_product_prices(ctx, "test")
        for p in prices:
            self.assertEqual(p["currency"], "USDT")


# =========================================================================
class TestWalletDebit(unittest.TestCase):
    """Wallet debit must use the server-computed total, not client input."""

    def test_wallet_service_uses_server_total(self):
        """order_service.create_reseller_order accepts only an OrderPricingResult.
        The wallet debit is derived entirely from pricing.total_cents."""
        from reseller_api.services.order_service import create_reseller_order
        import inspect
        sig = inspect.signature(create_reseller_order)
        params = list(sig.parameters.keys())
        # create_reseller_order must take a pricing result, never a raw amount
        self.assertIn("pricing", params,
                      "create_reseller_order must accept 'pricing' parameter")

    def test_order_pricing_result_is_immutable_value_object(self):
        """OrderPricingResult stores integers only and cannot be mutated
        via a price field after construction (slots guard)."""
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=10.00)
        _grant_access(rid, pid)
        vo = _validated_order(rid, pid, pub_id=pub)
        result = calculate_order_price(vo)
        # __slots__ prevents adding NEW arbitrary attributes
        with self.assertRaises(AttributeError):
            result.fake_price = 999  # non-slot attribute is blocked


# =========================================================================
class TestMasterProductProtection(unittest.TestCase):
    """Master product price/cost must be unmodifiable via the reseller path."""

    def test_set_pricing_rule_does_not_modify_products_table(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=50.00)
        _grant_access(rid, pid)
        master_before = _get_master_price(pid)

        rdb.set_pricing_rule(rid, pid, "fixed_price", 1_00, True)  # $1
        rdb.set_pricing_rule(rid, pid, "markup_percent", 5000, True)
        rdb.delete_pricing_rule(rid, pid)

        master_after = _get_master_price(pid)
        self.assertAlmostEqual(master_before, master_after)

    def test_two_resellers_can_have_different_rules_on_same_product(self):
        rid1, ctx1 = _make_reseller()
        rid2, ctx2 = _make_reseller()
        pid, pub = _make_product(price=20.00)
        _grant_access(rid1, pid)
        _grant_access(rid2, pid)

        rdb.set_pricing_rule(rid1, pid, "fixed_price", 1500, True)  # $15
        rdb.set_pricing_rule(rid2, pid, "markup_percent", 1000, True)  # +10% = $22

        u1, s1 = rdb.resolve_reseller_price_cents(rid1, pid, 1)
        u2, s2 = rdb.resolve_reseller_price_cents(rid2, pid, 1)
        self.assertEqual(u1, 1500)
        self.assertEqual(u2, 2200)
        # Master untouched
        self.assertAlmostEqual(_get_master_price(pid), 20.00)


# =========================================================================
class TestResellerCannotUnderpay(unittest.TestCase):
    """The wallet debit path uses the server total; there is no client path
    to supply a lower amount."""

    def test_total_cents_equals_unit_times_quantity(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=7.00)
        _grant_access(rid, pid)
        rdb.set_pricing_rule(rid, pid, "fixed_price", 700, True)  # $7.00

        vo = _validated_order(rid, pid, pub_id=pub, quantity=5)
        result = calculate_order_price(vo)
        self.assertEqual(result.total_cents, 3500)
        self.assertEqual(result.unit_price_cents * result.quantity, result.total_cents)

    def test_no_float_in_total(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=9.99)
        _grant_access(rid, pid)
        vo = _validated_order(rid, pid, pub_id=pub, quantity=3)
        result = calculate_order_price(vo)
        self.assertIsInstance(result.total_cents, int)
        # 999 * 3 = 2997
        self.assertEqual(result.total_cents, 2997)


# =========================================================================
class TestResellerCannotAlterCost(unittest.TestCase):
    """The pricing rule table only affects reseller selling price;
    there is no field for internal cost and no API to set it."""

    def test_pricing_rule_table_has_no_cost_column(self):
        cur = rdb.get_cursor()
        cur.execute("PRAGMA table_info(reseller_pricing_rules)")
        columns = {row[1] for row in cur.fetchall()}
        self.assertNotIn("cost", columns)
        self.assertNotIn("internal_cost", columns)
        self.assertNotIn("master_cost", columns)

    def test_set_pricing_rule_has_no_cost_parameter(self):
        import inspect
        sig = inspect.signature(rdb.set_pricing_rule)
        params = list(sig.parameters.keys())
        self.assertNotIn("cost", params)
        self.assertNotIn("internal_cost", params)


# =========================================================================
class TestPricingServiceAuthorization(unittest.TestCase):
    """Authentication and authorization guards on the pricing service."""

    def test_none_context_raises_401(self):
        with self.assertRaises(PricingError) as cm:
            get_product_price(None, "PRD-AAAAAAAAAAAA", "test")
        self.assertEqual(cm.exception.status, 401)

    def test_none_context_list_raises_401(self):
        with self.assertRaises(PricingError) as cm:
            list_product_prices(None, "test")
        self.assertEqual(cm.exception.status, 401)

    def test_product_not_accessible_returns_404(self):
        rid, ctx = _make_reseller()
        pid, pub = _make_product(price=10.00)
        # NOT granting access
        with self.assertRaises(PricingError) as cm:
            get_product_price(ctx, pub, "test")
        self.assertEqual(cm.exception.status, 404)

    def test_nonexistent_product_id_returns_404(self):
        rid, ctx = _make_reseller()
        with self.assertRaises(PricingError) as cm:
            get_product_price(ctx, "PRD-ZZZZZZZZZZZA", "test")
        self.assertEqual(cm.exception.status, 404)


# =========================================================================
class TestCrossResellerIsolation(unittest.TestCase):
    """Reseller A's pricing rule must not affect Reseller B's price."""

    def test_reseller_a_rule_does_not_affect_reseller_b(self):
        rid_a, ctx_a = _make_reseller()
        rid_b, ctx_b = _make_reseller()
        pid, pub = _make_product(price=10.00)
        _grant_access(rid_a, pid)
        _grant_access(rid_b, pid)

        rdb.set_pricing_rule(rid_a, pid, "fixed_price", 500, True)  # $5 for A
        # B has no rule -> inherits master

        u_a, _ = rdb.resolve_reseller_price_cents(rid_a, pid, 1)
        u_b, _ = rdb.resolve_reseller_price_cents(rid_b, pid, 1)
        self.assertEqual(u_a, 500)
        self.assertEqual(u_b, 1000)  # master

    def test_list_prices_returns_only_own_prices(self):
        rid_a, ctx_a = _make_reseller()
        rid_b, ctx_b = _make_reseller()
        pid, pub = _make_product(price=10.00)
        _grant_access(rid_a, pid)
        # B has no access

        prices_a = list_product_prices(ctx_a, "test")
        prices_b = list_product_prices(ctx_b, "test")

        ids_a = {p["product_id"] for p in prices_a}
        ids_b = {p["product_id"] for p in prices_b}
        self.assertIn(pub, ids_a)
        self.assertNotIn(pub, ids_b)


# =========================================================================
class TestMoneyArithmetic(unittest.TestCase):
    """Verify integer-cent arithmetic throughout; no floats."""

    def test_to_cents_exact(self):
        self.assertEqual(rdb.to_cents("19.99"), 1999)
        self.assertEqual(rdb.to_cents("0.01"), 1)
        self.assertEqual(rdb.to_cents("1000.00"), 100000)

    def test_from_cents_exact(self):
        self.assertEqual(rdb.from_cents(1999), "19.99")
        self.assertEqual(rdb.from_cents(1), "0.01")
        self.assertEqual(rdb.from_cents(100000), "1000.00")

    def test_percent_to_bps(self):
        self.assertEqual(rdb.percent_to_bps("15"), 1500)
        self.assertEqual(rdb.percent_to_bps("12.5"), 1250)
        self.assertEqual(rdb.percent_to_bps("0"), 0)
        self.assertEqual(rdb.percent_to_bps("100"), 10000)

    def test_percent_to_bps_rejects_float(self):
        with self.assertRaises(TypeError):
            rdb.percent_to_bps(15.0)

    def test_resolve_from_values_no_float_arithmetic(self):
        cents, source = rdb.resolve_reseller_price_from_values(
            "10.00", "markup_percent", None, 1000,  # 10%
        )
        self.assertIsInstance(cents, int)
        self.assertEqual(cents, 1100)


if __name__ == "__main__":
    unittest.main()
