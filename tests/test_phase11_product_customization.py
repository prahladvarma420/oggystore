"""
Phase 11 Prompt 4 — Reseller product customisation (service-level tests).
HTTP-layer tests live in api_v1_my_catalog_http_suite.py and are loaded
conditionally below (Flask may not be present in every validation env).

Service-level tests cover:
  - rename reseller product (custom_name)
  - description change (custom_description)
  - SKU change (reseller_sku)
  - enable / disable mapping (enabled)
  - master product name remains unchanged after reseller rename
  - another reseller's mapping remains unchanged (isolation)
  - invalid / non-existent mapping -> ProductImportError 404
  - PATCH forbidden master-product fields -> ProductImportError
  - master product deletion cascades mapping
  - list_mappings returns only the authenticated reseller's items
  - authorization: None context raises 401
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── Isolated DB ─────────────────────────────────────────────────────
_fd, _DB = tempfile.mkstemp(prefix="oggy_p11_cust_", suffix=".db")
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
    ProductImportError,
    get_mapping,
    import_product,
    list_mappings,
    remove_mapping,
    update_mapping,
    MAX_CUSTOM_NAME_LEN,
    MAX_CUSTOM_DESC_LEN,
    MAX_SKU_LEN,
)

# ── Counter ──────────────────────────────────────────────────────
_ctr = 0


def _uid():
    global _ctr
    _ctr += 1
    return 7_200_000 + _ctr


def _make_reseller():
    uid = _uid()
    n = _ctr
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"CustRS{n}", f"cust_rs_{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"CustReseller {n}", uid, f"cust_user_{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    ctx = _context(key_id, secret)
    return rs["id"], ctx


def _context(key_id, secret, env="test"):
    principal = authenticate_credentials(key_id, secret, env)
    assert principal is not None
    return ApiRequestContext(
        request_id="c" * 32,
        environment=principal.environment,
        reseller_id=principal.reseller_id,
        reseller_public_id=principal.reseller_public_id,
        api_key_id=principal.api_key_id,
        api_key_public_id=principal.api_key_public_id,
    )


def _make_product(name="Master Widget", price=10.0, active=1):
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active, description,"
        " validity, warranty, rules, redeem_link, max_per_order)"
        " VALUES(?,?,50,'code_stock',?,?,?,?,?,?,?)",
        (name, price, active, f"Desc {name}", "30d", "1y", "none", "no", 5),
    )
    db.conn.commit()
    pid = db.cur.lastrowid
    pub = rdb.ensure_product_public_id(pid)
    return pid, pub


def _grant(reseller_id, product_id):
    rdb.set_product_access(reseller_id, product_id, enabled=True)


def _use_this_db():
    """Re-point the shared database.db handle at THIS module's temp DB.

    The conditionally imported HTTP suite rebinds db.DB_PATH / db._tls to its
    own temp database at import time and deletes that file in its
    tearDownClass. Service-level test classes must therefore rebind before
    touching the database so they are independent of class execution order.
    """
    config.DB_PATH = _DB
    db.DB_PATH = _DB
    db._tls = threading.local()


# ────────────────────────────────────────────────────────────────────────────
class TestProductCustomizationService(unittest.TestCase):
    """Service-layer tests (no Flask required)."""

    def setUp(self):
        _use_this_db()
        self.pid, self.pub = _make_product()
        self.rs1, self.ctx1 = _make_reseller()
        _grant(self.rs1, self.pid)
        import_product(self.ctx1, self.pub, "test")

        self.rs2, self.ctx2 = _make_reseller()
        _grant(self.rs2, self.pid)
        import_product(self.ctx2, self.pub, "test")

    # — rename ———————————————————————————————————————————————
    def test_rename_reseller_product(self):
        result = update_mapping(self.ctx1, self.pub, "test", {"custom_name": "My Widget"})
        self.assertEqual(result["custom_name"], "My Widget")

    def test_rename_does_not_change_master_name(self):
        update_mapping(self.ctx1, self.pub, "test", {"custom_name": "Reseller Name"})
        prod = rdb.get_product_by_id(self.pid)
        self.assertEqual(prod["name"], "Master Widget",
                         "Master product name must not change after reseller rename")

    def test_rename_does_not_affect_other_reseller(self):
        update_mapping(self.ctx1, self.pub, "test", {"custom_name": "RS1 Name"})
        m2 = get_mapping(self.ctx2, self.pub, "test")
        self.assertIsNone(m2["custom_name"])

    def test_clear_custom_name(self):
        update_mapping(self.ctx1, self.pub, "test", {"custom_name": "Temp"})
        result = update_mapping(self.ctx1, self.pub, "test", {"custom_name": None})
        self.assertIsNone(result["custom_name"])

    def test_custom_name_too_long(self):
        with self.assertRaises(ProductImportError) as cm:
            update_mapping(self.ctx1, self.pub, "test",
                           {"custom_name": "x" * (MAX_CUSTOM_NAME_LEN + 1)})
        self.assertEqual(cm.exception.code, "invalid_field")

    # — description ————————————————————————————————————————————
    def test_change_description(self):
        result = update_mapping(self.ctx1, self.pub, "test",
                                {"custom_description": "Our exclusive widget."})
        self.assertEqual(result["custom_description"], "Our exclusive widget.")

    def test_description_does_not_change_master(self):
        update_mapping(self.ctx1, self.pub, "test", {"custom_description": "Custom"})
        prod = rdb.get_product_by_id(self.pid)
        self.assertEqual(prod["name"], "Master Widget")

    def test_description_does_not_affect_other_reseller(self):
        update_mapping(self.ctx1, self.pub, "test", {"custom_description": "RS1 desc"})
        m2 = get_mapping(self.ctx2, self.pub, "test")
        self.assertIsNone(m2["custom_description"])

    def test_description_too_long(self):
        with self.assertRaises(ProductImportError) as cm:
            update_mapping(self.ctx1, self.pub, "test",
                           {"custom_description": "y" * (MAX_CUSTOM_DESC_LEN + 1)})
        self.assertEqual(cm.exception.code, "invalid_field")

    # — SKU —————————————————————————————————————————————————
    def test_sku_change(self):
        result = update_mapping(self.ctx1, self.pub, "test", {"reseller_sku": "MY-SKU-001"})
        self.assertEqual(result["reseller_sku"], "MY-SKU-001")

    def test_sku_does_not_affect_master(self):
        update_mapping(self.ctx1, self.pub, "test", {"reseller_sku": "SKU-A"})
        prod = rdb.get_product_by_id(self.pid)
        self.assertEqual(prod["name"], "Master Widget")

    def test_sku_isolated_per_reseller(self):
        update_mapping(self.ctx1, self.pub, "test", {"reseller_sku": "SKU-A"})
        m2 = get_mapping(self.ctx2, self.pub, "test")
        self.assertIsNone(m2["reseller_sku"])

    def test_sku_clear(self):
        update_mapping(self.ctx1, self.pub, "test", {"reseller_sku": "TEMP"})
        result = update_mapping(self.ctx1, self.pub, "test", {"reseller_sku": None})
        self.assertIsNone(result["reseller_sku"])

    def test_sku_too_long(self):
        with self.assertRaises(ProductImportError) as cm:
            update_mapping(self.ctx1, self.pub, "test",
                           {"reseller_sku": "z" * (MAX_SKU_LEN + 1)})
        self.assertEqual(cm.exception.code, "invalid_field")

    # — enable / disable ——————————————————————————————————————————
    def test_disable_product(self):
        result = update_mapping(self.ctx1, self.pub, "test", {"enabled": False})
        self.assertFalse(result["enabled"])

    def test_enable_product(self):
        update_mapping(self.ctx1, self.pub, "test", {"enabled": False})
        result = update_mapping(self.ctx1, self.pub, "test", {"enabled": True})
        self.assertTrue(result["enabled"])

    def test_enable_not_boolean(self):
        with self.assertRaises(ProductImportError) as cm:
            update_mapping(self.ctx1, self.pub, "test", {"enabled": "yes"})
        self.assertEqual(cm.exception.code, "invalid_field")

    def test_disable_does_not_change_master_active(self):
        update_mapping(self.ctx1, self.pub, "test", {"enabled": False})
        prod = rdb.get_product_by_id(self.pid)
        self.assertTrue(bool(prod["active"]),
                        "Disabling reseller mapping must not deactivate master product")

    def test_disable_does_not_affect_other_reseller(self):
        update_mapping(self.ctx1, self.pub, "test", {"enabled": False})
        m2 = get_mapping(self.ctx2, self.pub, "test")
        self.assertTrue(m2["enabled"])

    # — multiple fields at once ————————————————————————————————————
    def test_patch_multiple_fields(self):
        result = update_mapping(self.ctx1, self.pub, "test", {
            "custom_name": "Bundle Widget",
            "reseller_sku": "BW-001",
            "enabled": True,
        })
        self.assertEqual(result["custom_name"], "Bundle Widget")
        self.assertEqual(result["reseller_sku"], "BW-001")
        self.assertTrue(result["enabled"])

    # — forbidden / master-product fields ———————————————————————————
    def test_cannot_patch_price(self):
        with self.assertRaises(ProductImportError) as cm:
            update_mapping(self.ctx1, self.pub, "test", {"price": 0.01})
        self.assertIn(cm.exception.code, ("invalid_field",))

    def test_cannot_patch_stock(self):
        with self.assertRaises(ProductImportError):
            update_mapping(self.ctx1, self.pub, "test", {"stock": 0})

    def test_cannot_patch_product_id(self):
        with self.assertRaises(ProductImportError):
            update_mapping(self.ctx1, self.pub, "test",
                           {"product_id": "PRD-XXXXXXXXXX00"})

    def test_cannot_patch_fulfillment(self):
        with self.assertRaises(ProductImportError):
            update_mapping(self.ctx1, self.pub, "test", {"fulfillment": "manual"})

    def test_unknown_field_rejected(self):
        with self.assertRaises(ProductImportError) as cm:
            update_mapping(self.ctx1, self.pub, "test", {"bogus": "x"})
        self.assertEqual(cm.exception.code, "invalid_field")

    def test_empty_fields_dict_is_noop(self):
        """Empty dict returns current mapping without error."""
        result = update_mapping(self.ctx1, self.pub, "test", {})
        self.assertEqual(result["product_id"], self.pub)

    # — not found / invalid IDs —————————————————————————————————
    def test_update_nonexistent_mapping(self):
        with self.assertRaises(ProductImportError) as cm:
            update_mapping(self.ctx1, "PRD-AAAAAAAAAA00", "test", {"custom_name": "X"})
        self.assertEqual(cm.exception.status, 404)

    def test_update_invalid_id_format(self):
        with self.assertRaises(ProductImportError) as cm:
            update_mapping(self.ctx1, "not-valid", "test", {"custom_name": "X"})
        self.assertEqual(cm.exception.status, 404)

    # — authorization ———————————————————————————————————————————
    def test_update_no_context(self):
        with self.assertRaises(ProductImportError) as cm:
            update_mapping(None, self.pub, "test", {"custom_name": "X"})
        self.assertEqual(cm.exception.status, 401)

    def test_list_no_context(self):
        with self.assertRaises(ProductImportError) as cm:
            list_mappings(None, "test")
        self.assertEqual(cm.exception.status, 401)

    def test_update_cannot_modify_other_resellers_mapping(self):
        """RS2 update affects RS2's own mapping only."""
        update_mapping(self.ctx2, self.pub, "test", {"custom_name": "RS2 Name"})
        m1 = get_mapping(self.ctx1, self.pub, "test")
        self.assertIsNone(m1["custom_name"])

    # — list_mappings ———————————————————————————————————————————
    def test_list_returns_imported_products(self):
        items = list_mappings(self.ctx1, "test")
        pids = [i["product_id"] for i in items]
        self.assertIn(self.pub, pids)

    def test_list_isolation(self):
        update_mapping(self.ctx1, self.pub, "test", {"custom_name": "RS1 Only"})
        items1 = list_mappings(self.ctx1, "test")
        items2 = list_mappings(self.ctx2, "test")
        names1 = [i.get("custom_name") for i in items1]
        names2 = [i.get("custom_name") for i in items2]
        self.assertIn("RS1 Only", names1)
        self.assertNotIn("RS1 Only", names2)

    def test_list_contains_master_name(self):
        items = list_mappings(self.ctx1, "test")
        self.assertTrue(any(i.get("master_name") == "Master Widget" for i in items))

    def test_list_no_internal_ids(self):
        items = list_mappings(self.ctx1, "test")
        for item in items:
            self.assertNotIn("reseller_id", item)
            # product_id in list should be the public PRD-* string
            self.assertTrue(str(item["product_id"]).startswith("PRD-"),
                            f"Expected public PRD-* id, got {item['product_id']!r}")

    # — deleted master product —————————————————————————————————
    def test_update_after_master_deleted(self):
        rdb.delete_product(self.pid)
        with self.assertRaises(ProductImportError) as cm:
            update_mapping(self.ctx1, self.pub, "test", {"custom_name": "Ghost"})
        self.assertEqual(cm.exception.status, 404)


    def test_list_after_master_deleted(self):
        rdb.delete_product(self.pid)
        items = list_mappings(self.ctx1, "test")
        self.assertEqual(items, [],
                         "list_mappings must return empty after master product is deleted")

    def test_delete_does_not_remove_master_product(self):
        remove_mapping(self.ctx1, self.pub, "test")
        prod = rdb.get_product_by_id(self.pid)
        self.assertIsNotNone(prod,
                             "Master product must still exist after reseller removes mapping")

    def test_delete_does_not_affect_other_reseller(self):
        remove_mapping(self.ctx1, self.pub, "test")
        m2 = get_mapping(self.ctx2, self.pub, "test")
        self.assertIsNotNone(m2)


# ── Optional HTTP-layer tests (require Flask) ──────────────────────────────

if importlib.util.find_spec("flask") is not None:
    from tests.api_v1_my_catalog_http_suite import MyCatalogHttpTests  # noqa: F401
else:
    class MyCatalogHttpTests(unittest.TestCase):
        @unittest.skip("Flask is not installed in this validation environment")
        def test_http_my_catalog_requires_flask(self):
            pass


if __name__ == "__main__":
    unittest.main()


class TestListAccessRevocation(unittest.TestCase):
    """When the owner revokes product access, list_mappings must stop
    disclosing live master commercial data (price / availability / category)
    while keeping the reseller's own mapping manageable."""

    def setUp(self):
        _use_this_db()
        self.pid, self.pub = _make_product()
        self.rs1, self.ctx1 = _make_reseller()
        _grant(self.rs1, self.pid)
        import_product(self.ctx1, self.pub, "test")

    def _entry(self):
        items = list_mappings(self.ctx1, "test")
        matches = [i for i in items if i["product_id"] == self.pub]
        self.assertEqual(len(matches), 1, "mapping row must remain listed")
        return matches[0]

    def test_access_enabled_shows_live_data(self):
        entry = self._entry()
        self.assertTrue(entry["access_enabled"])
        self.assertIsNotNone(entry["price"])
        self.assertIn(entry["availability"], ("in_stock", "out_of_stock"))

    def test_revoked_access_masks_live_master_data(self):
        rdb.set_product_access(self.rs1, self.pid, enabled=False)
        entry = self._entry()
        self.assertFalse(entry["access_enabled"])
        self.assertIsNone(entry["price"], "revoked access must not disclose live price")
        self.assertIsNone(entry["availability"], "revoked access must not disclose live stock")
        self.assertIsNone(entry["category"], "revoked access must not disclose live category")
        # The reseller's own mapping stays visible/manageable
        self.assertIn("custom_name", entry)
        self.assertTrue(entry["enabled"])

    def test_reenabled_access_restores_live_data(self):
        rdb.set_product_access(self.rs1, self.pid, enabled=False)
        rdb.set_product_access(self.rs1, self.pid, enabled=True)
        entry = self._entry()
        self.assertTrue(entry["access_enabled"])
        self.assertIsNotNone(entry["price"])
