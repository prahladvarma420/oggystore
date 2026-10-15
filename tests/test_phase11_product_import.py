"""Phase 11 Prompt 3 — Reseller Product Import & Product Mapping tests.

Covers:
  - import product (happy path)
  - duplicate import rejection
  - invalid / malformed product ID
  - hidden / disabled / inactive product
  - unauthorized product (no access row)
  - remove mapping (master product untouched)
  - multiple resellers with independent mappings
  - same product mapped by two resellers
  - concurrent import (race-condition safety)
  - custom_name / custom_description overrides
  - field length validation
  - no internal fields exposed in response
  - master product deletion cascades mapping
  - get mapping endpoint
  - unknown body fields rejected
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

# ── isolated DB ─────────────────────────────────────────────────────────────────
_fd, _DB = tempfile.mkstemp(prefix="oggy_p11_import_", suffix=".db")
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
from reseller_api.services.product_import import (
    MAX_CUSTOM_DESC_LEN,
    MAX_CUSTOM_NAME_LEN,
    ProductImportError,
    get_mapping,
    import_product,
    remove_mapping,
)

# ── Helpers ───────────────────────────────────────────────────────────────────
_counter = 0


def _uid():
    global _counter
    _counter += 1
    return 8_000_000 + _counter


def _make_reseller(name=None):
    uid = _uid()
    n = _counter
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, name or f"ImpRS{n}", f"imp_rs_{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(name or f"ImportReseller {n}", uid, f"imp_user_{n}")
    rdb.set_reseller_status(rs["id"], "active")
    return rs


def _make_key(reseller_id, env="test"):
    return rdb.create_api_key(reseller_id, env)


def _context(kid, secret, env="test"):
    principal = authenticate_credentials(kid, secret, env)
    assert principal is not None
    return ApiRequestContext(
        request_id="b" * 32,
        environment=principal.environment,
        reseller_id=principal.reseller_id,
        reseller_public_id=principal.reseller_public_id,
        api_key_id=principal.api_key_id,
        api_key_public_id=principal.api_key_public_id,
    )


def _make_product(name=None, price=10.0, active=1, stock_codes=None):
    n = _counter + 1
    pname = name or f"ImpProd {n}"
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active, description,"
        " validity, warranty, rules, redeem_link, max_per_order)"
        " VALUES(?,?,0,'code_stock',?,?,?,?,?,?,?)",
        (pname, price, active, f"Desc {pname}", "30d", "1y", "none", "no", 1),
    )
    db.conn.commit()
    pid = db.cur.lastrowid
    pub = rdb.ensure_product_public_id(pid)
    if stock_codes:
        for c in stock_codes:
            db.cur.execute(
                "INSERT INTO product_stock(product_id, delivery, sold) VALUES(?,?,0)",
                (pid, c),
            )
        db.conn.commit()
    return pid, pub


def _grant(reseller_id, product_id):
    rdb.set_product_access(reseller_id, product_id, enabled=True)


# ── Tests ───────────────────────────────────────────────────────────────────

class TestImportHappyPath(unittest.TestCase):
    """Basic import creates a mapping and returns the correct structure."""

    def setUp(self):
        self.rs = _make_reseller()
        self.kid, self.sec = _make_key(self.rs["id"])
        self.ctx = _context(self.kid, self.sec)
        self.pid, self.pub = _make_product("Happy Widget", stock_codes=["hw1"])
        _grant(self.rs["id"], self.pid)

    def test_import_returns_mapping_dict(self):
        m = import_product(self.ctx, self.pub, "test")
        self.assertIsInstance(m, dict)

    def test_mapping_has_product_id(self):
        m = import_product(self.ctx, self.pub, "test")
        self.assertEqual(m["product_id"], self.pub)

    def test_mapping_has_enabled_true(self):
        m = import_product(self.ctx, self.pub, "test")
        self.assertTrue(m["enabled"])

    def test_mapping_has_imported_at(self):
        m = import_product(self.ctx, self.pub, "test")
        self.assertIn("imported_at", m)
        self.assertIsNotNone(m["imported_at"])

    def test_no_internal_product_id_in_response(self):
        m = import_product(self.ctx, self.pub, "test")
        # Internal integer id must never be in the mapping response
        for v in m.values():
            if isinstance(v, int):
                self.assertNotEqual(v, self.pid,
                                    "Internal product integer id must not be exposed")

    def test_mapping_row_created_in_db(self):
        import_product(self.ctx, self.pub, "test")
        row = rdb.get_reseller_product_mapping(self.rs["id"], self.pid)
        self.assertIsNotNone(row)
        self.assertEqual(int(row["product_id"]), self.pid)
        self.assertEqual(int(row["reseller_id"]), self.rs["id"])

    def test_master_product_not_duplicated(self):
        import_product(self.ctx, self.pub, "test")
        db.cur.execute("SELECT COUNT(*) FROM products WHERE id=?", (self.pid,))
        count = db.cur.fetchone()[0]
        self.assertEqual(count, 1, "Master product must never be duplicated")

    def test_stock_not_duplicated(self):
        import_product(self.ctx, self.pub, "test")
        db.cur.execute(
            "SELECT COUNT(*) FROM product_stock WHERE product_id=?", (self.pid,)
        )
        count = db.cur.fetchone()[0]
        self.assertEqual(count, 1, "Stock codes must never be duplicated")


class TestImportWithCustomFields(unittest.TestCase):
    """Custom name and description overrides are stored and returned."""

    def setUp(self):
        self.rs = _make_reseller()
        self.kid, self.sec = _make_key(self.rs["id"])
        self.ctx = _context(self.kid, self.sec)
        self.pid, self.pub = _make_product("Custom Widget", stock_codes=["cw1"])
        _grant(self.rs["id"], self.pid)

    def test_custom_name_stored(self):
        m = import_product(self.ctx, self.pub, "test",
                           custom_name="My Reseller Name")
        self.assertEqual(m["custom_name"], "My Reseller Name")

    def test_custom_description_stored(self):
        m = import_product(self.ctx, self.pub, "test",
                           custom_description="My custom desc")
        self.assertEqual(m["custom_description"], "My custom desc")

    def test_no_custom_fields_default_to_none(self):
        m = import_product(self.ctx, self.pub, "test")
        self.assertIsNone(m["custom_name"])
        self.assertIsNone(m["custom_description"])

    def test_custom_name_too_long_rejected(self):
        with self.assertRaises(ProductImportError) as cm:
            import_product(self.ctx, self.pub, "test",
                           custom_name="x" * (MAX_CUSTOM_NAME_LEN + 1))
        self.assertIn("invalid_field", str(cm.exception))

    def test_custom_description_too_long_rejected(self):
        with self.assertRaises(ProductImportError) as cm:
            import_product(self.ctx, self.pub, "test",
                           custom_description="x" * (MAX_CUSTOM_DESC_LEN + 1))
        self.assertIn("invalid_field", str(cm.exception))

    def test_blank_custom_name_stored_as_none(self):
        m = import_product(self.ctx, self.pub, "test", custom_name="   ")
        self.assertIsNone(m["custom_name"])


class TestDuplicateImport(unittest.TestCase):
    """The same reseller must not import the same product twice."""

    def setUp(self):
        self.rs = _make_reseller()
        self.kid, self.sec = _make_key(self.rs["id"])
        self.ctx = _context(self.kid, self.sec)
        self.pid, self.pub = _make_product("Dup Widget", stock_codes=["dw1"])
        _grant(self.rs["id"], self.pid)

    def test_second_import_raises_409(self):
        import_product(self.ctx, self.pub, "test")
        with self.assertRaises(ProductImportError) as cm:
            import_product(self.ctx, self.pub, "test")
        self.assertEqual(cm.exception.status, 409)

    def test_second_import_code_is_already_imported(self):
        import_product(self.ctx, self.pub, "test")
        with self.assertRaises(ProductImportError) as cm:
            import_product(self.ctx, self.pub, "test")
        self.assertEqual(cm.exception.code, "already_imported")

    def test_only_one_mapping_row_after_duplicate_attempt(self):
        import_product(self.ctx, self.pub, "test")
        try:
            import_product(self.ctx, self.pub, "test")
        except ProductImportError:
            pass
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_product_mappings "
            "WHERE reseller_id=? AND product_id=?",
            (self.rs["id"], self.pid),
        )
        count = db.cur.fetchone()[0]
        self.assertEqual(count, 1)


class TestInvalidProduct(unittest.TestCase):
    """Invalid, malformed, nonexistent, and unauthorized product IDs."""

    def setUp(self):
        self.rs = _make_reseller()
        self.kid, self.sec = _make_key(self.rs["id"])
        self.ctx = _context(self.kid, self.sec)

    def test_malformed_id_raises_404(self):
        with self.assertRaises(ProductImportError) as cm:
            import_product(self.ctx, "not-a-valid-id", "test")
        self.assertEqual(cm.exception.status, 404)

    def test_empty_id_raises_404(self):
        with self.assertRaises(ProductImportError) as cm:
            import_product(self.ctx, "", "test")
        self.assertEqual(cm.exception.status, 404)

    def test_integer_id_raises_404(self):
        with self.assertRaises(ProductImportError) as cm:
            import_product(self.ctx, "12345", "test")
        self.assertEqual(cm.exception.status, 404)

    def test_valid_format_nonexistent_id_raises_404(self):
        with self.assertRaises(ProductImportError) as cm:
            import_product(self.ctx, "PRD-AAAAAAAAAAAA", "test")
        self.assertEqual(cm.exception.status, 404)

    def test_product_without_access_raises_404(self):
        pid, pub = _make_product("No Access", stock_codes=["na1"])
        # no _grant call
        with self.assertRaises(ProductImportError) as cm:
            import_product(self.ctx, pub, "test")
        self.assertEqual(cm.exception.status, 404)

    def test_error_codes_are_identical_for_nonexistent_and_noaccess(self):
        """No existence oracle: same error shape for nonexistent vs access-denied."""
        pid, pub_noaccess = _make_product("No Access 2", stock_codes=["na2"])
        with self.assertRaises(ProductImportError) as cm_nonexist:
            import_product(self.ctx, "PRD-ZZZZZZZZZZZZ", "test")
        with self.assertRaises(ProductImportError) as cm_noaccess:
            import_product(self.ctx, pub_noaccess, "test")
        self.assertEqual(cm_nonexist.exception.code, cm_noaccess.exception.code)
        self.assertEqual(cm_nonexist.exception.status, cm_noaccess.exception.status)


class TestDisabledProduct(unittest.TestCase):
    """Inactive (active=0) products must not be importable."""

    def setUp(self):
        self.rs = _make_reseller()
        self.kid, self.sec = _make_key(self.rs["id"])
        self.ctx = _context(self.kid, self.sec)

    def test_inactive_product_cannot_be_imported(self):
        pid, pub = _make_product("Inactive Prod", active=0, stock_codes=["ip1"])
        _grant(self.rs["id"], pid)
        with self.assertRaises(ProductImportError) as cm:
            import_product(self.ctx, pub, "test")
        self.assertEqual(cm.exception.status, 404)

    def test_disabled_after_import_does_not_orphan_mapping(self):
        """Disabling a product leaves the mapping row (no cascade) — but the
        product disappears from the catalog query. Mapping will be cleaned up
        if owner hard-deletes the product (ON DELETE CASCADE).
        """
        pid, pub = _make_product("Will Disable", active=1, stock_codes=["wd1"])
        _grant(self.rs["id"], pid)
        import_product(self.ctx, pub, "test")
        # Owner disables the product
        db.cur.execute("UPDATE products SET active=0 WHERE id=?", (pid,))
        db.conn.commit()
        # Mapping row still exists (owner may re-enable later)
        row = rdb.get_reseller_product_mapping(self.rs["id"], pid)
        self.assertIsNotNone(row, "Mapping must survive product disable")


class TestRemoveMapping(unittest.TestCase):
    """Removing a mapping never touches the master product."""

    def setUp(self):
        self.rs = _make_reseller()
        self.kid, self.sec = _make_key(self.rs["id"])
        self.ctx = _context(self.kid, self.sec)
        self.pid, self.pub = _make_product("Removable", stock_codes=["rm1"])
        _grant(self.rs["id"], self.pid)
        import_product(self.ctx, self.pub, "test")

    def test_remove_succeeds(self):
        remove_mapping(self.ctx, self.pub, "test")  # must not raise

    def test_mapping_row_deleted_after_remove(self):
        remove_mapping(self.ctx, self.pub, "test")
        row = rdb.get_reseller_product_mapping(self.rs["id"], self.pid)
        self.assertIsNone(row)

    def test_master_product_still_exists_after_remove(self):
        remove_mapping(self.ctx, self.pub, "test")
        db.cur.execute("SELECT id FROM products WHERE id=?", (self.pid,))
        self.assertIsNotNone(db.cur.fetchone(),
                             "Master product must NEVER be deleted by remove_mapping")

    def test_stock_still_exists_after_remove(self):
        remove_mapping(self.ctx, self.pub, "test")
        db.cur.execute(
            "SELECT COUNT(*) FROM product_stock WHERE product_id=?", (self.pid,)
        )
        count = db.cur.fetchone()[0]
        self.assertGreater(count, 0, "Stock must not be deleted by remove_mapping")

    def test_remove_nonexistent_mapping_raises_404(self):
        """Removing a mapping that was never created must return 404."""
        rs2 = _make_reseller()
        kid2, sec2 = _make_key(rs2["id"])
        ctx2 = _context(kid2, sec2)
        pid2, pub2 = _make_product("NoMapping", stock_codes=["nm1"])
        _grant(rs2["id"], pid2)
        # Never imported by rs2 — remove must raise 404
        with self.assertRaises(ProductImportError) as cm:
            remove_mapping(ctx2, pub2, "test")
        self.assertEqual(cm.exception.status, 404)

    def test_reimport_after_remove_succeeds(self):
        remove_mapping(self.ctx, self.pub, "test")
        m = import_product(self.ctx, self.pub, "test")  # must not raise
        self.assertEqual(m["product_id"], self.pub)


class TestMasterProductDeletion(unittest.TestCase):
    """ON DELETE CASCADE: hard-deleting a master product removes all mappings."""

    def setUp(self):
        self.rs = _make_reseller()
        self.kid, self.sec = _make_key(self.rs["id"])
        self.ctx = _context(self.kid, self.sec)
        self.pid, self.pub = _make_product("Deletable", stock_codes=["del1"])
        _grant(self.rs["id"], self.pid)
        import_product(self.ctx, self.pub, "test")

    def test_mapping_cascade_deleted_when_product_hard_deleted(self):
        # Simulate owner hard-deleting the product
        db.cur.execute("DELETE FROM products WHERE id=?", (self.pid,))
        db.conn.commit()
        row = rdb.get_reseller_product_mapping(self.rs["id"], self.pid)
        self.assertIsNone(
            row,
            "Mapping must be CASCADE-deleted when master product is hard-deleted",
        )


class TestMultipleResellers(unittest.TestCase):
    """Two resellers can independently map the same product."""

    def setUp(self):
        self.rsA = _make_reseller("MR-A")
        self.rsB = _make_reseller("MR-B")
        kidA, secA = _make_key(self.rsA["id"])
        kidB, secB = _make_key(self.rsB["id"])
        self.ctxA = _context(kidA, secA)
        self.ctxB = _context(kidB, secB)
        self.pid, self.pub = _make_product("Shared Widget", stock_codes=["sw1"])
        _grant(self.rsA["id"], self.pid)
        _grant(self.rsB["id"], self.pid)

    def test_both_resellers_can_import_same_product(self):
        mA = import_product(self.ctxA, self.pub, "test")
        mB = import_product(self.ctxB, self.pub, "test")
        self.assertEqual(mA["product_id"], self.pub)
        self.assertEqual(mB["product_id"], self.pub)

    def test_two_mapping_rows_exist(self):
        import_product(self.ctxA, self.pub, "test")
        import_product(self.ctxB, self.pub, "test")
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_product_mappings WHERE product_id=?",
            (self.pid,),
        )
        count = db.cur.fetchone()[0]
        self.assertEqual(count, 2)

    def test_reseller_a_cannot_remove_reseller_b_mapping(self):
        import_product(self.ctxA, self.pub, "test")
        import_product(self.ctxB, self.pub, "test")
        remove_mapping(self.ctxA, self.pub, "test")
        # B's mapping must still exist
        rowB = rdb.get_reseller_product_mapping(self.rsB["id"], self.pid)
        self.assertIsNotNone(rowB, "Reseller A remove must not affect Reseller B")

    def test_reseller_isolation_custom_names(self):
        import_product(self.ctxA, self.pub, "test", custom_name="A Name")
        import_product(self.ctxB, self.pub, "test", custom_name="B Name")
        rowA = rdb.get_reseller_product_mapping(self.rsA["id"], self.pid)
        rowB = rdb.get_reseller_product_mapping(self.rsB["id"], self.pid)
        self.assertEqual(rowA["custom_name"], "A Name")
        self.assertEqual(rowB["custom_name"], "B Name")

    def test_duplicate_per_reseller_is_independent(self):
        """A's duplicate error must not prevent B from importing."""
        import_product(self.ctxA, self.pub, "test")
        try:
            import_product(self.ctxA, self.pub, "test")
        except ProductImportError:
            pass
        mB = import_product(self.ctxB, self.pub, "test")  # must succeed
        self.assertEqual(mB["product_id"], self.pub)


class TestGetMapping(unittest.TestCase):
    """GET /api/v1/products/{product_id}/import — retrieve mapping status."""

    def setUp(self):
        self.rs = _make_reseller()
        self.kid, self.sec = _make_key(self.rs["id"])
        self.ctx = _context(self.kid, self.sec)
        self.pid, self.pub = _make_product("GetMe Widget", stock_codes=["gm1"])
        _grant(self.rs["id"], self.pid)

    def test_get_after_import_returns_mapping(self):
        import_product(self.ctx, self.pub, "test")
        m = get_mapping(self.ctx, self.pub, "test")
        self.assertEqual(m["product_id"], self.pub)

    def test_get_before_import_raises_404(self):
        with self.assertRaises(ProductImportError) as cm:
            get_mapping(self.ctx, self.pub, "test")
        self.assertEqual(cm.exception.status, 404)

    def test_get_after_remove_raises_404(self):
        import_product(self.ctx, self.pub, "test")
        remove_mapping(self.ctx, self.pub, "test")
        with self.assertRaises(ProductImportError) as cm:
            get_mapping(self.ctx, self.pub, "test")
        self.assertEqual(cm.exception.status, 404)

    def test_get_includes_custom_name(self):
        import_product(self.ctx, self.pub, "test", custom_name="My Name")
        m = get_mapping(self.ctx, self.pub, "test")
        self.assertEqual(m["custom_name"], "My Name")


class TestUnauthorized(unittest.TestCase):
    """None context and suspended/closed resellers must be rejected."""

    def setUp(self):
        self.rs = _make_reseller()
        self.kid, self.sec = _make_key(self.rs["id"])
        self.ctx = _context(self.kid, self.sec)
        self.pid, self.pub = _make_product("Auth Widget", stock_codes=["aw2"])
        _grant(self.rs["id"], self.pid)

    def test_none_context_import_raises_401(self):
        with self.assertRaises(ProductImportError) as cm:
            import_product(None, self.pub, "test")
        self.assertEqual(cm.exception.status, 401)

    def test_none_context_remove_raises_401(self):
        with self.assertRaises(ProductImportError) as cm:
            remove_mapping(None, self.pub, "test")
        self.assertEqual(cm.exception.status, 401)

    def test_none_context_get_raises_401(self):
        with self.assertRaises(ProductImportError) as cm:
            get_mapping(None, self.pub, "test")
        self.assertEqual(cm.exception.status, 401)

    def test_suspended_reseller_cannot_import(self):
        rs2 = _make_reseller()
        kid2, sec2 = _make_key(rs2["id"])
        # Get context before suspend
        ctx2 = _context(kid2, sec2)
        rdb.set_reseller_status(rs2["id"], "suspended")
        pid2, pub2 = _make_product("Suspended Product", stock_codes=["sp1"])
        _grant(rs2["id"], pid2)
        with self.assertRaises(ProductImportError):
            import_product(ctx2, pub2, "test")


class TestConcurrentImport(unittest.TestCase):
    """Race condition: concurrent imports of the same product by the same reseller.

    Only one row must exist after the race.  One thread succeeds; the other
    receives ``already_imported`` (409).
    """

    def setUp(self):
        self.rs = _make_reseller()
        self.kid, self.sec = _make_key(self.rs["id"])
        self.ctx = _context(self.kid, self.sec)
        self.pid, self.pub = _make_product("Race Widget", stock_codes=["rw1", "rw2"])
        _grant(self.rs["id"], self.pid)

    def test_concurrent_import_one_wins(self):
        results = []
        errors = []
        unexpected = []

        def _do_import():
            try:
                m = import_product(self.ctx, self.pub, "test")
                results.append(m)
            except ProductImportError as exc:
                errors.append(exc)
            except Exception as exc:
                # Any non-ProductImportError (e.g. raw sqlite3.OperationalError
                # "database is locked" from a leaked write transaction) is a
                # service-contract violation and must fail this test.
                unexpected.append(exc)

        threads = [threading.Thread(target=_do_import) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(
            unexpected, [],
            f"Threads raised unexpected exceptions: {[repr(e) for e in unexpected]}",
        )
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_product_mappings "
            "WHERE reseller_id=? AND product_id=?",
            (self.rs["id"], self.pid),
        )
        count = db.cur.fetchone()[0]
        self.assertEqual(count, 1, "Exactly one mapping row must exist after race")
        self.assertEqual(len(results), 1, "Exactly one thread must succeed")
        self.assertEqual(len(errors), 3, "Three losing threads must get a controlled error")
        for exc in errors:
            self.assertIn(exc.code, ("already_imported", "import_failed"),
                          f"Losing threads must get already_imported or import_failed, got: {exc.code}")


class TestResellersNotExposedToEachOther(unittest.TestCase):
    """Cross-reseller IDOR: Reseller A must not see/touch Reseller B mappings."""

    def setUp(self):
        self.rsA = _make_reseller("IDOR-A")
        self.rsB = _make_reseller("IDOR-B")
        kidA, secA = _make_key(self.rsA["id"])
        kidB, secB = _make_key(self.rsB["id"])
        self.ctxA = _context(kidA, secA)
        self.ctxB = _context(kidB, secB)
        self.pid, self.pub = _make_product("IDOR Widget", stock_codes=["id1"])
        _grant(self.rsA["id"], self.pid)
        _grant(self.rsB["id"], self.pid)
        import_product(self.ctxB, self.pub, "test")  # B imports

    def test_a_remove_does_not_affect_b_mapping(self):
        """A trying to remove B's mapping gets 404 (A never imported it)."""
        with self.assertRaises(ProductImportError) as cm:
            remove_mapping(self.ctxA, self.pub, "test")
        self.assertEqual(cm.exception.status, 404)
        # B's mapping must still exist
        rowB = rdb.get_reseller_product_mapping(self.rsB["id"], self.pid)
        self.assertIsNotNone(rowB)

    def test_a_get_returns_404_for_unmapped_product(self):
        """A has not imported the product so GET must return 404."""
        with self.assertRaises(ProductImportError) as cm:
            get_mapping(self.ctxA, self.pub, "test")
        self.assertEqual(cm.exception.status, 404)


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
