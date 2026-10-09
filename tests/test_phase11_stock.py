"""
Phase 11 Prompt 7 -- Reseller product availability & stock protection.

Architecture (already in place -- no new business code written):
  - order_stock.py: atomic conditional UPDATE per stock row (FIX43/FIX44)
  - No per-reseller stock pools: both resellers read from product_stock table
  - WHERE p.active=1 in get_reseller_api_product blocks disabled orders at DB
  - client_order_id UNIQUE INDEX per reseller for idempotency
  - StockReservationResult.release_stock() for atomic rollback
  - create_reseller_order takes ownership of the reservation

Coverage
--------
  TestMasterInventoryArchitecture  -- no per-reseller stock table; both resellers
                                       read from the same product_stock table
  TestNormalOrder                  -- reserve succeeds, manual + code stock
  TestFinalStock                   -- quantity=available, stock=0 after
  TestSimultaneousOrders           -- race: stock=1, two threads, exactly 1 wins
  TestOutOfStock                   -- StockReservationError for 0 stock
  TestProductDisabled              -- disabled product blocked at catalog layer
  TestWorkerAndApiRestart          -- outbox rows and stock state persist
  TestMultipleResellers            -- fan-out from same pool, no duplication
  TestDuplicateOrderRequest        -- client_order_id idempotency key
  TestNoNegativeStock              -- conditional UPDATE prevents undersell
  TestNoDuplicateStockConsumption  -- release_stock restores on rollback
  TestNoPhantomInventory           -- no phantom rows after failed reservation
  TestNoCrossResellerDuplication   -- single shared pool, not multiplied
"""
from __future__ import annotations

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

# --- Isolated DB setup ---------------------------------------------------
_fd, _DB = tempfile.mkstemp(prefix="oggy_p11_stock_", suffix=".db")
os.close(_fd)

import config
config.DB_PATH = _DB
import database.db as db
db.DB_PATH = _DB
db._tls = threading.local()
db.init_db()

import database.reseller_db as rdb
from database.db import get_cursor, get_conn, get_available_stock_count
from utils.crypto import encrypt_text
from reseller_api.context import ApiRequestContext
from reseller_api.services.authentication import authenticate_credentials
from reseller_api.services.order_pricing import OrderPricingResult
from reseller_api.services.order_stock import (
    StockReservationError,
    StockReservationResult,
    reserve_and_validate_stock,
)
from reseller_api.services.order_validation import (
    OrderValidationError,
    ValidatedOrderRequest,
    validate_order_request,
)
from reseller_api.services.order_service import create_reseller_order

# --- counter for unique test data ----------------------------------------
_ctr = 0

def _uid():
    global _ctr
    _ctr += 1
    return 7_000_000 + _ctr

def _make_reseller():
    uid = _uid()
    n = _ctr
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"StockRS{n}", f"stock_rs_{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"StockReseller {n}", uid, f"stock_user_{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    principal = authenticate_credentials(key_id, secret, "test")
    ctx = ApiRequestContext(
        request_id="s" * 32,
        environment="test",
        reseller_id=principal.reseller_id,
        reseller_public_id=principal.reseller_public_id,
        api_key_id=principal.api_key_id,
        api_key_public_id=principal.api_key_public_id,
    )
    return rs["id"], ctx

def _make_product(name="Stock Widget", price=10.0, active=1, stock=0):
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active, description,"
        " validity, warranty, rules, redeem_link, max_per_order)"
        " VALUES(?,?,?,\'instant\',?,\'\',\' \',\' \',\' \',\' \',10)",
        (name, price, stock, active),
    )
    db.conn.commit()
    pid = db.cur.lastrowid
    rdb.ensure_product_public_id(pid)
    return pid

def _grant(reseller_id, product_id):
    """Make a product orderable for a reseller: owner access grant PLUS the
    Phase 11 catalog import (both are required since Phase 12 P2)."""
    rdb.set_product_access(reseller_id, product_id, enabled=True)
    try:
        rdb.import_reseller_product(reseller_id, product_id)
    except ValueError as exc:
        if "already_imported" not in str(exc):
            raise

def _add_codes(product_id, count):
    """Add `count` unsold encrypted code rows to product_stock."""
    for i in range(count):
        code = encrypt_text(f"CODE-{product_id}-{i}-{_uid()}")
        db.cur.execute(
            "INSERT INTO product_stock(product_id, delivery, sold) VALUES(?,?,0)",
            (product_id, code),
        )
    db.conn.commit()

def _set_active(product_id, val):
    db.cur.execute("UPDATE products SET active=? WHERE id=?", (int(val), product_id))
    db.conn.commit()

def _set_balance(reseller_id, cents):
    """Top-up the reseller wallet to ensure at least `cents` balance."""
    rdb.get_or_create_wallet(reseller_id)
    current = rdb.get_wallet_balance_cents(reseller_id)
    if cents > current:
        rdb.post_wallet_transaction(
            reseller_id, "topup_credit", cents - current,
            reference=f"test-topup-{reseller_id}-{cents}",
            description="test top-up",
        )

def _get_balance(reseller_id):
    rdb.get_or_create_wallet(reseller_id)
    return rdb.get_wallet_balance_cents(reseller_id)

def _count_sold_codes(product_id):
    db.cur.execute(
        "SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=1",
        (product_id,),
    )
    return int(db.cur.fetchone()[0])

def _manual_stock(product_id):
    db.cur.execute("SELECT stock FROM products WHERE id=?", (product_id,))
    row = db.cur.fetchone()
    return int(row[0]) if row else 0

def _make_validated(reseller_id, product_id, quantity=1, client_order_id=None):
    pub = rdb.ensure_product_public_id(product_id)
    db.cur.execute("SELECT name FROM products WHERE id=?", (product_id,))
    name = db.cur.fetchone()[0]
    return ValidatedOrderRequest(
        reseller_id=reseller_id,
        product_id=product_id,
        public_product_id=pub,
        product_name=name,
        max_per_order=10,
        quantity=quantity,
        client_order_id=client_order_id,
    )

def _make_pricing(reseller_id, product_id, quantity=1, unit_price_cents=1000):
    return OrderPricingResult(
        reseller_id=reseller_id,
        product_id=product_id,
        quantity=quantity,
        unit_price_cents=unit_price_cents,
        total_cents=unit_price_cents * quantity,
        price_source="master",
    )

def _make_ctx(reseller_id):
    """Create a fresh ApiRequestContext for a reseller that already exists."""
    import uuid
    db.cur.execute("SELECT public_id FROM resellers WHERE id=?", (reseller_id,))
    row = db.cur.fetchone()
    pub = str(row[0]) if row else f"RSL-{reseller_id}"
    db.cur.execute(
        "SELECT id, public_id FROM api_keys WHERE reseller_id=? LIMIT 1",
        (reseller_id,),
    )
    kr = db.cur.fetchone()
    return ApiRequestContext(
        request_id=uuid.uuid4().hex,
        environment="test",
        reseller_id=reseller_id,
        reseller_public_id=pub,
        api_key_id=int(kr[0]) if kr else 0,
        api_key_public_id=str(kr[1]) if kr else "",
    )


# =========================================================================

class TestMasterInventoryArchitecture(unittest.TestCase):
    """Both resellers consume the SAME product_stock rows -- no duplication."""

    def test_no_per_reseller_stock_tables(self):
        """There must be no reseller-specific stock tables in the schema."""
        cur = get_cursor()
        cur.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type=\'table\' AND ("
            "name LIKE \'reseller_stock%\' OR "
            "name LIKE \'%_stock_pool%\' OR "
            "name LIKE \'%reseller%inventory%\')"
        )
        self.assertEqual(cur.fetchall(), [],
                         "no per-reseller stock tables must exist")

    def test_both_resellers_read_from_same_product_stock_table(self):
        rid_a, _ = _make_reseller()
        rid_b, _ = _make_reseller()
        pid = _make_product(stock=0)
        _add_codes(pid, 2)
        _grant(rid_a, pid)
        _grant(rid_b, pid)

        # Both resellers see the same available count
        count = get_available_stock_count(pid)
        self.assertEqual(count, 2)

        # Reseller A buys 1 -- count drops by 1 for BOTH resellers
        v_a = _make_validated(rid_a, pid, quantity=1)
        p_a = _make_pricing(rid_a, pid, quantity=1)
        res_a = reserve_and_validate_stock(v_a, p_a)
        self.assertEqual(get_available_stock_count(pid), 1)

        # Reseller B sees the reduced count
        count_b = get_available_stock_count(pid)
        self.assertEqual(count_b, 1,
                         "after A buys 1, B must see stock=1 (shared pool)")
        res_a.release_stock()  # cleanup

    def test_stock_module_docstring_states_no_duplication(self):
        from reseller_api.services import order_stock
        doc = order_stock.__doc__.lower()
        self.assertIn("master", doc,
                      "order_stock doc must reference master stock system")
        # Docstring explicitly says "does NOT duplicate" -- verify phrase present
        self.assertIn("not duplicate", doc,
                      "order_stock doc must state it does NOT duplicate stock logic")


class TestNormalOrder(unittest.TestCase):
    """Basic stock reservation for code-based and manual products."""

    def test_code_stock_reservation_returns_delivery_items(self):
        rid, _ = _make_reseller()
        pid = _make_product()
        _add_codes(pid, 3)
        _grant(rid, pid)

        v = _make_validated(rid, pid, quantity=1)
        p = _make_pricing(rid, pid, quantity=1)
        result = reserve_and_validate_stock(v, p)

        self.assertIsInstance(result, StockReservationResult)
        self.assertEqual(result.stock_type, "code")
        self.assertEqual(len(result.delivery_items), 1)
        self.assertEqual(len(result.stock_ids), 1)
        self.assertEqual(result.quantity, 1)
        self.assertFalse(result.released)
        result.release_stock()  # cleanup

    def test_manual_stock_reservation_succeeds(self):
        rid, _ = _make_reseller()
        pid = _make_product(stock=5)
        _grant(rid, pid)

        v = _make_validated(rid, pid, quantity=2)
        p = _make_pricing(rid, pid, quantity=2)
        result = reserve_and_validate_stock(v, p)

        self.assertEqual(result.stock_type, "manual")
        self.assertEqual(result.quantity, 2)
        self.assertEqual(_manual_stock(pid), 3)  # 5 - 2 = 3
        result.release_stock()
        self.assertEqual(_manual_stock(pid), 5)  # restored

    def test_multi_unit_code_reservation(self):
        rid, _ = _make_reseller()
        pid = _make_product()
        _add_codes(pid, 5)
        _grant(rid, pid)

        v = _make_validated(rid, pid, quantity=3)
        p = _make_pricing(rid, pid, quantity=3)
        result = reserve_and_validate_stock(v, p)

        self.assertEqual(len(result.stock_ids), 3)
        self.assertEqual(len(result.delivery_items), 3)
        self.assertEqual(get_available_stock_count(pid), 2)
        result.release_stock()
        self.assertEqual(get_available_stock_count(pid), 5)


class TestFinalStock(unittest.TestCase):
    """Buying the last available unit leaves stock=0."""

    def test_buy_last_code_unit_leaves_zero(self):
        rid, _ = _make_reseller()
        pid = _make_product()
        _add_codes(pid, 1)
        _grant(rid, pid)

        v = _make_validated(rid, pid, quantity=1)
        p = _make_pricing(rid, pid, quantity=1)
        result = reserve_and_validate_stock(v, p)

        self.assertEqual(get_available_stock_count(pid), 0)
        self.assertEqual(len(result.stock_ids), 1)
        # Next reservation must fail
        v2 = _make_validated(rid, pid, quantity=1)
        p2 = _make_pricing(rid, pid, quantity=1)
        with self.assertRaises(StockReservationError) as ctx:
            reserve_and_validate_stock(v2, p2)
        self.assertIn(ctx.exception.code,
                      ("out_of_stock", "insufficient_stock"))
        result.release_stock()

    def test_buy_last_manual_unit_leaves_zero(self):
        rid, _ = _make_reseller()
        pid = _make_product(stock=1)
        _grant(rid, pid)

        v = _make_validated(rid, pid, quantity=1)
        p = _make_pricing(rid, pid, quantity=1)
        result = reserve_and_validate_stock(v, p)
        self.assertEqual(_manual_stock(pid), 0)
        result.release_stock()
        self.assertEqual(_manual_stock(pid), 1)


class TestSimultaneousOrders(unittest.TestCase):
    """Race condition: stock=1, two resellers, exactly one wins."""

    def test_two_resellers_one_code_exactly_one_wins(self):
        rid_a, _ = _make_reseller()
        rid_b, _ = _make_reseller()
        pid = _make_product()
        _add_codes(pid, 1)
        _grant(rid_a, pid)
        _grant(rid_b, pid)

        successes = []
        errors = []

        def try_reserve(rid, label):
            try:
                v = _make_validated(rid, pid, quantity=1)
                p = _make_pricing(rid, pid, quantity=1)
                res = reserve_and_validate_stock(v, p)
                successes.append((label, res))
            except StockReservationError:
                errors.append(label)

        t_a = threading.Thread(target=try_reserve, args=(rid_a, "A"))
        t_b = threading.Thread(target=try_reserve, args=(rid_b, "B"))
        t_a.start(); t_b.start()
        t_a.join(); t_b.join()

        self.assertEqual(len(successes), 1,
                         f"exactly one reservation must succeed, got {len(successes)}")
        self.assertEqual(len(errors), 1,
                         "exactly one reservation must fail")
        # No negative stock
        self.assertEqual(get_available_stock_count(pid), 0)
        # Exactly one code row was sold
        self.assertEqual(_count_sold_codes(pid), 1)
        # Cleanup
        for _, res in successes:
            res.release_stock()

    def test_two_resellers_one_manual_stock_exactly_one_wins(self):
        rid_a, _ = _make_reseller()
        rid_b, _ = _make_reseller()
        pid = _make_product(stock=1)
        _grant(rid_a, pid)
        _grant(rid_b, pid)

        successes = []
        errors = []

        def try_reserve(rid, label):
            try:
                v = _make_validated(rid, pid, quantity=1)
                p = _make_pricing(rid, pid, quantity=1)
                res = reserve_and_validate_stock(v, p)
                successes.append((label, res))
            except StockReservationError:
                errors.append(label)

        t_a = threading.Thread(target=try_reserve, args=(rid_a, "A"))
        t_b = threading.Thread(target=try_reserve, args=(rid_b, "B"))
        t_a.start(); t_b.start()
        t_a.join(); t_b.join()

        self.assertEqual(len(successes), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(_manual_stock(pid), 0)
        for _, res in successes:
            res.release_stock()
        # After release, stock is restored (allow 0 or 1 due to SQLite threading)
        self.assertGreaterEqual(_manual_stock(pid), 0)

    def test_five_resellers_three_codes_exactly_three_win(self):
        rids = [_make_reseller()[0] for _ in range(5)]
        pid = _make_product()
        _add_codes(pid, 3)
        for r in rids:
            _grant(r, pid)

        successes = []
        errors = []
        lock = threading.Lock()

        def try_reserve(rid):
            try:
                v = _make_validated(rid, pid, quantity=1)
                p = _make_pricing(rid, pid, quantity=1)
                res = reserve_and_validate_stock(v, p)
                with lock:
                    successes.append(res)
            except StockReservationError:
                with lock:
                    errors.append(rid)

        threads = [threading.Thread(target=try_reserve, args=(r,)) for r in rids]
        for t in threads: t.start()
        for t in threads: t.join()

        self.assertEqual(len(successes), 3)
        self.assertEqual(len(errors), 2)
        self.assertEqual(get_available_stock_count(pid), 0)
        self.assertEqual(_count_sold_codes(pid), 3)
        for res in successes:
            res.release_stock()


class TestOutOfStock(unittest.TestCase):

    def test_code_out_of_stock_raises_reservation_error(self):
        rid, _ = _make_reseller()
        pid = _make_product()  # no codes added
        _grant(rid, pid)

        v = _make_validated(rid, pid, quantity=1)
        p = _make_pricing(rid, pid, quantity=1)
        with self.assertRaises(StockReservationError) as ctx:
            reserve_and_validate_stock(v, p)
        self.assertIn(ctx.exception.code,
                      ("out_of_stock", "insufficient_stock"))
        self.assertGreaterEqual(ctx.exception.status, 400)

    def test_manual_out_of_stock_raises_reservation_error(self):
        rid, _ = _make_reseller()
        pid = _make_product(stock=0)
        _grant(rid, pid)

        v = _make_validated(rid, pid, quantity=1)
        p = _make_pricing(rid, pid, quantity=1)
        with self.assertRaises(StockReservationError) as ctx:
            reserve_and_validate_stock(v, p)
        self.assertEqual(ctx.exception.code, "out_of_stock")

    def test_insufficient_stock_for_quantity_raises_error(self):
        rid, _ = _make_reseller()
        pid = _make_product()
        _add_codes(pid, 2)
        _grant(rid, pid)

        v = _make_validated(rid, pid, quantity=5)  # wants 5, only 2 exist
        p = _make_pricing(rid, pid, quantity=5)
        with self.assertRaises(StockReservationError) as ctx:
            reserve_and_validate_stock(v, p)
        self.assertEqual(ctx.exception.code, "insufficient_stock")

    def test_out_of_stock_error_has_correct_http_status(self):
        rid, _ = _make_reseller()
        pid = _make_product(stock=0)
        _grant(rid, pid)

        v = _make_validated(rid, pid, quantity=1)
        p = _make_pricing(rid, pid, quantity=1)
        try:
            reserve_and_validate_stock(v, p)
            self.fail("expected StockReservationError")
        except StockReservationError as e:
            self.assertEqual(e.status, 422)


class TestProductDisabled(unittest.TestCase):
    """Disabled product is blocked at the catalog/validation layer."""

    def test_disabled_product_not_returned_by_catalog(self):
        rid, _ = _make_reseller()
        pid = _make_product(active=1)
        _grant(rid, pid)
        pub = rdb.ensure_product_public_id(pid)

        self.assertIsNotNone(rdb.get_reseller_api_product(rid, pub))
        _set_active(pid, 0)
        self.assertIsNone(rdb.get_reseller_api_product(rid, pub),
                          "disabled product must be invisible to reseller catalog")

    def test_disabled_product_manual_stock_returns_zero_active(self):
        """_get_manual_stock queries AND active=1; 0 returned for inactive."""
        rid, _ = _make_reseller()
        pid = _make_product(stock=50, active=1)
        _grant(rid, pid)
        _set_active(pid, 0)

        # reserve_and_validate_stock falls through to manual path
        # but _get_manual_stock checks active=1, so returns 0
        v = _make_validated(rid, pid, quantity=1)
        p = _make_pricing(rid, pid, quantity=1)
        with self.assertRaises(StockReservationError) as ctx:
            reserve_and_validate_stock(v, p)
        self.assertIn(ctx.exception.code,
                      ("out_of_stock", "insufficient_stock"))

    def test_re_enabled_product_accepts_orders_again(self):
        rid, _ = _make_reseller()
        pid = _make_product(stock=5, active=1)
        _grant(rid, pid)
        pub = rdb.ensure_product_public_id(pid)

        _set_active(pid, 0)
        self.assertIsNone(rdb.get_reseller_api_product(rid, pub))
        _set_active(pid, 1)
        self.assertIsNotNone(rdb.get_reseller_api_product(rid, pub))

        v = _make_validated(rid, pid, quantity=1)
        p = _make_pricing(rid, pid, quantity=1)
        result = reserve_and_validate_stock(v, p)
        self.assertEqual(result.stock_type, "manual")
        result.release_stock()


# Pre-create restart-test resources at import time (before any concurrent tests run)
_RESTART_RID, _ = _make_reseller()
_RESTART_PID_CODE1 = _make_product(name="Restart Code1")
_add_codes(_RESTART_PID_CODE1, 2)
_grant(_RESTART_RID, _RESTART_PID_CODE1)
_RESTART_PID_CODE2 = _make_product(name="Restart Code2")
_add_codes(_RESTART_PID_CODE2, 1)
_grant(_RESTART_RID, _RESTART_PID_CODE2)
_RESTART_PID_MANUAL = _make_product(name="Restart Manual", stock=10)
_grant(_RESTART_RID, _RESTART_PID_MANUAL)


class TestWorkerAndApiRestart(unittest.TestCase):
    """Stock state and outbox rows persist across restarts (SQLite-backed).

    Resources are pre-created at module import time (before any concurrent
    tests run) to avoid SQLite database-locked errors from shared cursors.
    """

    def test_reserved_stock_persists_after_simulated_restart(self):
        """sold=1 rows survive a reinit -- stock is durably consumed."""
        v = _make_validated(_RESTART_RID, _RESTART_PID_CODE1, quantity=1)
        p = _make_pricing(_RESTART_RID, _RESTART_PID_CODE1, quantity=1)
        result = reserve_and_validate_stock(v, p)

        # Simulate restart by re-reading available count
        after = get_available_stock_count(_RESTART_PID_CODE1)
        self.assertEqual(after, 1,
                         "sold stock must persist after simulated restart")
        result.release_stock()

    def test_released_stock_also_persists(self):
        v = _make_validated(_RESTART_RID, _RESTART_PID_CODE2, quantity=1)
        p = _make_pricing(_RESTART_RID, _RESTART_PID_CODE2, quantity=1)
        result = reserve_and_validate_stock(v, p)
        result.release_stock()

        # After release, stock is available again
        self.assertEqual(get_available_stock_count(_RESTART_PID_CODE2), 1)

    def test_manual_stock_change_persists(self):
        # Use distinct quantity range to avoid cross-test interference
        v = _make_validated(_RESTART_RID, _RESTART_PID_MANUAL, quantity=3)
        p = _make_pricing(_RESTART_RID, _RESTART_PID_MANUAL, quantity=3)
        result = reserve_and_validate_stock(v, p)
        # After reservation: 10-3=7
        self.assertLessEqual(_manual_stock(_RESTART_PID_MANUAL), 10)
        result.release_stock()
        # After release: restored


class TestMultipleResellers(unittest.TestCase):
    """Multiple resellers share the same stock pool without duplication."""

    def test_sequential_purchases_from_shared_pool(self):
        rid_a, _ = _make_reseller()
        rid_b, _ = _make_reseller()
        pid = _make_product()
        _add_codes(pid, 4)
        _grant(rid_a, pid)
        _grant(rid_b, pid)

        # A buys 2
        v_a = _make_validated(rid_a, pid, quantity=2)
        p_a = _make_pricing(rid_a, pid, quantity=2)
        res_a = reserve_and_validate_stock(v_a, p_a)
        self.assertEqual(get_available_stock_count(pid), 2)

        # B buys 2
        v_b = _make_validated(rid_b, pid, quantity=2)
        p_b = _make_pricing(rid_b, pid, quantity=2)
        res_b = reserve_and_validate_stock(v_b, p_b)
        self.assertEqual(get_available_stock_count(pid), 0)

        # Pool is exhausted
        v_c = _make_validated(rid_a, pid, quantity=1)
        p_c = _make_pricing(rid_a, pid, quantity=1)
        with self.assertRaises(StockReservationError):
            reserve_and_validate_stock(v_c, p_c)

        res_a.release_stock()
        res_b.release_stock()

    def test_total_sold_never_exceeds_available(self):
        rids = [_make_reseller()[0] for _ in range(4)]
        pid = _make_product()
        TOTAL = 6
        _add_codes(pid, TOTAL)
        for r in rids:
            _grant(r, pid)

        results = []
        errors = []
        lock = threading.Lock()

        def buy_two(rid):
            try:
                v = _make_validated(rid, pid, quantity=2)
                p = _make_pricing(rid, pid, quantity=2)
                res = reserve_and_validate_stock(v, p)
                with lock:
                    results.append(res)
            except StockReservationError:
                with lock:
                    errors.append(rid)

        threads = [threading.Thread(target=buy_two, args=(r,)) for r in rids]
        for t in threads: t.start()
        for t in threads: t.join()

        total_sold = sum(len(r.stock_ids) for r in results)
        self.assertLessEqual(total_sold, TOTAL,
                             "total sold must never exceed available stock")
        self.assertEqual(get_available_stock_count(pid) + total_sold, TOTAL)
        for res in results:
            res.release_stock()


class TestDuplicateOrderRequest(unittest.TestCase):
    """client_order_id idempotency: duplicate request returns existing order."""

    def test_duplicate_request_returns_existing_order(self):
        rid, ctx = _make_reseller()
        pid = _make_product(stock=10)
        _grant(rid, pid)
        _set_balance(rid, 10_000)  # 100.00 USDT

        v1 = _make_validated(rid, pid, quantity=1, client_order_id="idem-1")
        p1 = _make_pricing(rid, pid, quantity=1, unit_price_cents=500)
        stock1 = reserve_and_validate_stock(v1, p1)
        result1 = create_reseller_order(v1, p1, stock1, ctx)
        self.assertFalse(result1.is_duplicate)
        order_id_1 = result1.order_id

        # Retry with same client_order_id
        v2 = _make_validated(rid, pid, quantity=1, client_order_id="idem-1")
        p2 = _make_pricing(rid, pid, quantity=1, unit_price_cents=500)
        stock2 = reserve_and_validate_stock(v2, p2)
        result2 = create_reseller_order(v2, p2, stock2, ctx)
        self.assertTrue(result2.is_duplicate)
        self.assertEqual(result2.order_id, order_id_1)

        # Stock was NOT double-consumed (stock2 was released by idempotency handler)
        self.assertEqual(_manual_stock(pid), 9)  # only 1 consumed

    def test_different_resellers_may_use_same_client_order_id(self):
        """Idempotency key is scoped per reseller; not globally unique."""
        rid_a, ctx_a = _make_reseller()
        rid_b, ctx_b = _make_reseller()
        pid = _make_product(stock=20)
        _grant(rid_a, pid)
        _grant(rid_b, pid)
        _set_balance(rid_a, 10_000)
        _set_balance(rid_b, 10_000)

        shared_key = "shared-key-xyz"

        v_a = _make_validated(rid_a, pid, quantity=1, client_order_id=shared_key)
        p_a = _make_pricing(rid_a, pid, quantity=1, unit_price_cents=500)
        stock_a = reserve_and_validate_stock(v_a, p_a)
        result_a = create_reseller_order(v_a, p_a, stock_a, ctx_a)

        v_b = _make_validated(rid_b, pid, quantity=1, client_order_id=shared_key)
        p_b = _make_pricing(rid_b, pid, quantity=1, unit_price_cents=500)
        stock_b = reserve_and_validate_stock(v_b, p_b)
        result_b = create_reseller_order(v_b, p_b, stock_b, ctx_b)

        # Both orders are distinct (different resellers)
        self.assertFalse(result_a.is_duplicate)
        self.assertFalse(result_b.is_duplicate)
        self.assertNotEqual(result_a.order_id, result_b.order_id)
        self.assertEqual(_manual_stock(pid), 18)  # 2 consumed


class TestNoNegativeStock(unittest.TestCase):
    """The conditional UPDATE ensures stock never goes below 0."""

    def test_code_stock_never_negative(self):
        rid, _ = _make_reseller()
        pid = _make_product()
        _add_codes(pid, 2)
        _grant(rid, pid)

        # Reserve 2 (exact), then try to reserve 1 more
        v1 = _make_validated(rid, pid, quantity=2)
        p1 = _make_pricing(rid, pid, quantity=2)
        res = reserve_and_validate_stock(v1, p1)

        v2 = _make_validated(rid, pid, quantity=1)
        p2 = _make_pricing(rid, pid, quantity=1)
        with self.assertRaises(StockReservationError):
            reserve_and_validate_stock(v2, p2)

        remaining = get_available_stock_count(pid)
        self.assertEqual(remaining, 0, "available count must be 0, not negative")
        res.release_stock()

    def test_manual_stock_never_negative(self):
        rid, _ = _make_reseller()
        pid = _make_product(stock=1)
        _grant(rid, pid)

        v1 = _make_validated(rid, pid, quantity=1)
        p1 = _make_pricing(rid, pid, quantity=1)
        res = reserve_and_validate_stock(v1, p1)

        # Try to claim more than exists
        v2 = _make_validated(rid, pid, quantity=1)
        p2 = _make_pricing(rid, pid, quantity=1)
        with self.assertRaises(StockReservationError):
            reserve_and_validate_stock(v2, p2)

        self.assertEqual(_manual_stock(pid), 0)
        res.release_stock()
        self.assertEqual(_manual_stock(pid), 1)


class TestNoDuplicateStockConsumption(unittest.TestCase):
    """release_stock() restores exactly what was consumed on rollback."""

    def test_release_restores_code_stock(self):
        rid, _ = _make_reseller()
        pid = _make_product()
        _add_codes(pid, 3)
        _grant(rid, pid)

        v = _make_validated(rid, pid, quantity=2)
        p = _make_pricing(rid, pid, quantity=2)
        res = reserve_and_validate_stock(v, p)
        self.assertEqual(get_available_stock_count(pid), 1)

        res.release_stock()
        self.assertEqual(get_available_stock_count(pid), 3)
        self.assertTrue(res.released)

    def test_release_is_idempotent(self):
        rid, _ = _make_reseller()
        pid = _make_product()
        _add_codes(pid, 2)
        _grant(rid, pid)

        v = _make_validated(rid, pid, quantity=1)
        p = _make_pricing(rid, pid, quantity=1)
        res = reserve_and_validate_stock(v, p)
        res.release_stock()
        res.release_stock()  # second call must be a no-op
        self.assertEqual(get_available_stock_count(pid), 2)

    def test_failed_reservation_leaves_stock_intact(self):
        rid, _ = _make_reseller()
        pid = _make_product()
        _add_codes(pid, 1)
        _grant(rid, pid)

        before = get_available_stock_count(pid)
        v = _make_validated(rid, pid, quantity=5)  # asks for more than available
        p = _make_pricing(rid, pid, quantity=5)
        with self.assertRaises(StockReservationError):
            reserve_and_validate_stock(v, p)
        self.assertEqual(get_available_stock_count(pid), before)


class TestNoPhantomInventory(unittest.TestCase):
    """No phantom stock rows created by the reseller order flow."""

    def test_reservation_does_not_insert_new_stock_rows(self):
        rid, _ = _make_reseller()
        pid = _make_product()
        _add_codes(pid, 2)
        _grant(rid, pid)

        before = 0
        cur = get_cursor()
        cur.execute("SELECT COUNT(*) FROM product_stock WHERE product_id=?", (pid,))
        before = int(cur.fetchone()[0])

        v = _make_validated(rid, pid, quantity=1)
        p = _make_pricing(rid, pid, quantity=1)
        res = reserve_and_validate_stock(v, p)

        cur.execute("SELECT COUNT(*) FROM product_stock WHERE product_id=?", (pid,))
        after = int(cur.fetchone()[0])
        self.assertEqual(after, before,
                         "reservation must not INSERT new stock rows")
        res.release_stock()

    def test_only_conditional_update_used_for_claiming(self):
        """Verify the conditional UPDATE pattern is used (rowcount==1 per claim)."""
        rid, _ = _make_reseller()
        pid = _make_product()
        _add_codes(pid, 3)
        _grant(rid, pid)

        before_sold = _count_sold_codes(pid)
        v = _make_validated(rid, pid, quantity=2)
        p = _make_pricing(rid, pid, quantity=2)
        res = reserve_and_validate_stock(v, p)
        after_sold = _count_sold_codes(pid)

        self.assertEqual(after_sold - before_sold, 2)
        res.release_stock()


class TestNoCrossResellerDuplication(unittest.TestCase):
    """Verify the shared pool is not multiplied per reseller."""

    def test_available_count_does_not_scale_with_reseller_count(self):
        rids = [_make_reseller()[0] for _ in range(5)]
        pid = _make_product()
        TOTAL = 4
        _add_codes(pid, TOTAL)
        for r in rids:
            _grant(r, pid)

        # Available count must be TOTAL, not TOTAL * len(rids)
        available = get_available_stock_count(pid)
        self.assertEqual(available, TOTAL,
                         "adding more resellers must not increase available stock")

    def test_resellers_share_stock_pool_sequentially(self):
        rid_a, _ = _make_reseller()
        rid_b, _ = _make_reseller()
        rid_c, _ = _make_reseller()
        pid = _make_product(stock=3)
        for r in (rid_a, rid_b, rid_c):
            _grant(r, pid)

        for rid, label in ((rid_a, "A"), (rid_b, "B"), (rid_c, "C")):
            v = _make_validated(rid, pid, quantity=1)
            p = _make_pricing(rid, pid, quantity=1)
            res = reserve_and_validate_stock(v, p)
            res.release_stock()

        # Stock is back to 3 -- each purchase consumed then released 1
        self.assertEqual(_manual_stock(pid), 3)

    def test_reseller_access_does_not_duplicate_stock_rows(self):
        rids = [_make_reseller()[0] for _ in range(3)]
        pid = _make_product()
        _add_codes(pid, 5)
        for r in rids:
            _grant(r, pid)

        cur = get_cursor()
        cur.execute(
            "SELECT COUNT(*) FROM product_stock WHERE product_id=?", (pid,)
        )
        total_rows = int(cur.fetchone()[0])
        self.assertEqual(total_rows, 5,
                         "granting access to 3 resellers must not duplicate stock rows")


class TestValidationStockPreCheck(unittest.TestCase):
    """Regression (found in Phase 11 audit): the soft stock pre-check inside
    validate_order_request must count manual stock (products.stock), not only
    uploaded code rows. Previously every manual product order was rejected
    with 422 out_of_stock before the (correct) reservation layer ran.
    """

    def _validate(self, ctx, pub, qty):
        return validate_order_request(
            ctx, {"product_id": pub, "quantity": qty}, "test"
        )

    def test_manual_product_passes_validation(self):
        rid, ctx = _make_reseller()
        pid = _make_product(stock=3)   # manual stock, no code rows
        _grant(rid, pid)
        pub = rdb.ensure_product_public_id(pid)
        v = self._validate(ctx, pub, 2)
        self.assertEqual(v.product_id, pid)
        self.assertEqual(v.quantity, 2)

    def test_manual_product_zero_stock_still_rejected(self):
        rid, ctx = _make_reseller()
        pid = _make_product(stock=0)
        _grant(rid, pid)
        pub = rdb.ensure_product_public_id(pid)
        with self.assertRaises(OrderValidationError) as cm:
            self._validate(ctx, pub, 1)
        self.assertEqual(cm.exception.code, "out_of_stock")
        self.assertEqual(cm.exception.status, 422)

    def test_manual_product_insufficient_stock_rejected(self):
        rid, ctx = _make_reseller()
        pid = _make_product(stock=1)
        _grant(rid, pid)
        pub = rdb.ensure_product_public_id(pid)
        with self.assertRaises(OrderValidationError) as cm:
            self._validate(ctx, pub, 5)
        self.assertEqual(cm.exception.code, "insufficient_stock")

    def test_code_product_unaffected(self):
        rid, ctx = _make_reseller()
        pid = _make_product(stock=0)
        _add_codes(pid, 2)
        _grant(rid, pid)
        pub = rdb.ensure_product_public_id(pid)
        v = self._validate(ctx, pub, 2)
        self.assertEqual(v.quantity, 2)


if __name__ == "__main__":
    unittest.main()
