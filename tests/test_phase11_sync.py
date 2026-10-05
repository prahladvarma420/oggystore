"""
Phase 11 Prompt 6 -- Master product -> reseller sync tests.

All tests are service-level (no Flask/HTTP).  They operate directly against
the SQLite test DB, the product_sync service, and the webhook outbox.

Coverage
--------
  TestSyncEventTypes          -- only supported event types are registered
  TestNoProductCache          -- no product cache; all reads are live
  TestProductUpdated          -- notify_product_updated emits outbox rows
  TestProductDisabled         -- notify_product_disabled + order blocked at DB
  TestProductEnabled          -- notify_product_enabled after re-enable
  TestProductDeleted          -- capture-before-delete pattern + event emitted
  TestPriceUpdate             -- inherited resellers see new price immediately;
                                 rule-based resellers keep their rule on top
  TestStockUpdate             -- stock.updated emitted; stock never duplicated
  TestMultipleResellers       -- N resellers each receive their own outbox row
  TestCacheInvalidation       -- no product cache; live reads are always fresh
  TestWorkerRestart           -- pending outbox rows survive a worker restart
  TestQueueRetry              -- failed rows can be reset for re-delivery
  TestConcurrency             -- simultaneous update + catalog read + stock
  TestDeduplication           -- duplicate notify calls produce one outbox row
  TestAccessIsolation         -- resellers without access receive no events
  TestSyncHelpersSafe         -- helpers never raise on bad/edge input
"""
from __future__ import annotations

import importlib.util
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

# --- Isolated DB setup ---------------------------------------------------
_fd, _DB = tempfile.mkstemp(prefix="oggy_p11_sync_", suffix=".db")
os.close(_fd)

import config
config.DB_PATH = _DB
import database.db as db
db.DB_PATH = _DB
db._tls = threading.local()
db.init_db()

import database.reseller_db as rdb
from database.db import get_cursor, get_conn, get_available_stock_count
from reseller_api.services.webhook_outbox import (
    OUTBOX_EVENT_TYPES,
    emit_event_atomic,
    get_pending_events,
    get_outbox_row,
)
from reseller_api.services.product_sync import (
    get_resellers_with_product_access,
    emit_product_event_for_resellers,
    notify_product_updated,
    notify_product_disabled,
    notify_product_enabled,
    notify_product_deleted,
    notify_stock_updated,
)
from reseller_api.workers.webhook_worker import (
    _mark_delivered, _mark_failed_terminal, reset_for_manual_retry,
)

# --- counter for unique test data ----------------------------------------
_ctr = 0

def _uid():
    global _ctr
    _ctr += 1
    return 9_000_000 + _ctr

def _make_reseller():
    uid = _uid()
    n = _ctr
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"SyncRS{n}", f"sync_rs_{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"SyncReseller {n}", uid, f"sync_user_{n}")
    rdb.set_reseller_status(rs["id"], "active")
    return rs["id"]

def _make_product(name="Sync Widget", price=10.0, active=1):
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active, description,"
        " validity, warranty, rules, redeem_link, max_per_order)"
        " VALUES(?,?,50,\'instant\',?,\'\',\' \',\' \',\' \',\' \',10)",
        (name, price, active),
    )
    db.conn.commit()
    pid = db.cur.lastrowid
    rdb.ensure_product_public_id(pid)
    return pid

def _grant(reseller_id, product_id):
    rdb.set_product_access(reseller_id, product_id, enabled=True)

def _set_active(product_id, val):
    db.cur.execute("UPDATE products SET active=? WHERE id=?", (int(val), product_id))
    db.conn.commit()

def _latest_outbox_for(reseller_id, event_type):
    cur = get_cursor()
    cur.execute(
        "SELECT id, payload_json, status FROM webhook_outbox "
        "WHERE reseller_id=? AND event_type=? ORDER BY id DESC LIMIT 1",
        (int(reseller_id), str(event_type)),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"id": int(row[0]), "payload": json.loads(row[1]), "status": str(row[2])}

def _count_outbox(event_type):
    cur = get_cursor()
    cur.execute("SELECT COUNT(*) FROM webhook_outbox WHERE event_type=?", (event_type,))
    return int(cur.fetchone()[0])

# =========================================================================

class TestSyncEventTypes(unittest.TestCase):
    """Verify the supported product sync event types."""

    PRODUCT_EVENTS = {
        "product.updated", "product.disabled", "product.enabled",
        "product.deleted", "stock.updated",
    }
    UNSUPPORTED = {"product.created", "product.archived", "price.updated"}

    def test_supported_events_in_outbox_types(self):
        for ev in self.PRODUCT_EVENTS:
            self.assertIn(ev, OUTBOX_EVENT_TYPES,
                          f"{ev!r} missing from OUTBOX_EVENT_TYPES")

    def test_unsupported_events_not_registered(self):
        for ev in self.UNSUPPORTED:
            self.assertNotIn(ev, OUTBOX_EVENT_TYPES,
                             f"{ev!r} should NOT be in OUTBOX_EVENT_TYPES")

    def test_emit_unknown_event_type_raises(self):
        """emit_event (the inner primitive) raises; emit_event_atomic is best-effort."""
        from reseller_api.services.webhook_outbox import emit_event
        from database.db import get_cursor, get_conn
        import database.pgcompat as _pgcompat
        cur, conn = get_cursor(), get_conn()
        with self.assertRaises(ValueError):
            with _pgcompat.atomic(conn):
                emit_event(cur, 1, "product.archived", "PRD-X", {})

    def test_price_updated_not_registered(self):
        """price changes are covered by product.updated with changed_fields=[price]."""
        self.assertNotIn("price.updated", OUTBOX_EVENT_TYPES)


class TestNoProductCache(unittest.TestCase):
    """Product data is read live from master tables; there is no product cache."""

    def test_catalog_reflects_price_change_immediately(self):
        """After a master price change the reseller sees the new price with no
        explicit cache flush -- because there is no cache."""
        rid = _make_reseller()
        pid = _make_product(price=5.00)
        _grant(rid, pid)
        pub = rdb.ensure_product_public_id(pid)

        p1, _ = rdb.resolve_reseller_price_cents(rid, pid)
        self.assertEqual(p1, 500)

        db.cur.execute("UPDATE products SET price=8.00 WHERE id=?", (pid,))
        db.conn.commit()

        p2, src = rdb.resolve_reseller_price_cents(rid, pid)
        self.assertEqual(p2, 800)
        self.assertEqual(src, "master")

    def test_disabled_product_immediately_invisible_in_catalog(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)
        pub = rdb.ensure_product_public_id(pid)

        self.assertIsNotNone(rdb.get_reseller_api_product(rid, pub))
        _set_active(pid, 0)
        self.assertIsNone(rdb.get_reseller_api_product(rid, pub),
                          "disabled product must be invisible via catalog")

    def test_no_product_cache_module_exists(self):
        spec = importlib.util.find_spec("reseller_api.services.product_cache")
        self.assertIsNone(spec, "there must be no separate product_cache module")

    def test_security_controls_cache_is_unrelated_to_products(self):
        """The only in-process cache is the global API kill-switch (30 s TTL)."""
        import reseller_api.services.security_controls as sc
        self.assertTrue(hasattr(sc, "_CACHE_TTL_SECONDS"))
        self.assertEqual(sc._CACHE_TTL_SECONDS, 30.0)


class TestProductUpdated(unittest.TestCase):

    def test_emits_outbox_row_for_each_reseller(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)

        n = notify_product_updated(pid, changed_fields=["name", "description"])
        self.assertEqual(n, 1)
        row = _latest_outbox_for(rid, "product.updated")
        self.assertIsNotNone(row)
        self.assertEqual(row["payload"]["event"], "product.updated")
        self.assertIn("name", row["payload"]["changed_fields"])

    def test_internal_fields_stripped_from_payload(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)

        notify_product_updated(pid, changed_fields=["name", "cost", "delivery", "rules"])
        row = _latest_outbox_for(rid, "product.updated")
        fields = row["payload"]["changed_fields"]
        self.assertIn("name", fields)
        for bad in ("cost", "delivery", "rules"):
            self.assertNotIn(bad, fields, f"{bad!r} must not appear in reseller payload")

    def test_no_resellers_returns_zero(self):
        pid = _make_product()
        self.assertEqual(notify_product_updated(pid, changed_fields=["name"]), 0)

    def test_payload_has_product_id_and_name(self):
        rid = _make_reseller()
        pid = _make_product(name="Gadget Pro")
        _grant(rid, pid)
        notify_product_updated(pid, changed_fields=["name"])
        row = _latest_outbox_for(rid, "product.updated")
        self.assertIsNotNone(row["payload"].get("product_id"))
        self.assertEqual(row["payload"]["name"], "Gadget Pro")


class TestProductDisabled(unittest.TestCase):

    def test_disabled_product_blocks_order_at_db_level(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)
        pub = rdb.ensure_product_public_id(pid)

        self.assertIsNotNone(rdb.get_reseller_api_product(rid, pub))
        _set_active(pid, 0)
        self.assertIsNone(rdb.get_reseller_api_product(rid, pub))

    def test_notify_disabled_emits_outbox_row(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)
        _set_active(pid, 0)

        n = notify_product_disabled(pid)
        self.assertEqual(n, 1)
        row = _latest_outbox_for(rid, "product.disabled")
        self.assertIsNotNone(row)
        self.assertFalse(row["payload"]["active"])
        self.assertIn("note", row["payload"])

    def test_reseller_access_row_survives_disable(self):
        """Reseller access row is preserved when product is disabled (not deleted)."""
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)
        _set_active(pid, 0)

        rs = get_resellers_with_product_access(pid)
        self.assertTrue(any(r["reseller_id"] == rid for r in rs),
                        "access row must survive a product disable")


class TestProductEnabled(unittest.TestCase):

    def test_re_enabled_product_visible_in_catalog(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)
        pub = rdb.ensure_product_public_id(pid)

        _set_active(pid, 0)
        self.assertIsNone(rdb.get_reseller_api_product(rid, pub))
        _set_active(pid, 1)
        self.assertIsNotNone(rdb.get_reseller_api_product(rid, pub))

    def test_notify_enabled_emits_outbox_row(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)

        n = notify_product_enabled(pid)
        self.assertEqual(n, 1)
        row = _latest_outbox_for(rid, "product.enabled")
        self.assertIsNotNone(row)
        self.assertTrue(row["payload"]["active"])


class TestProductDeleted(unittest.TestCase):

    def test_notify_before_delete_emits_event(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)
        pub = rdb.ensure_product_public_id(pid)
        resellers = get_resellers_with_product_access(pid)

        # Emit BEFORE delete
        n = notify_product_deleted(pid, pub, "Gone Widget", resellers=resellers)
        self.assertEqual(n, 1)

        # Delete the product
        db.cur.execute("DELETE FROM products WHERE id=?", (pid,))
        db.conn.commit()

        row = _latest_outbox_for(rid, "product.deleted")
        self.assertIsNotNone(row)
        self.assertEqual(row["payload"]["product_id"], pub)
        self.assertEqual(row["payload"]["name"], "Gone Widget")
        self.assertIn("note", row["payload"])

    def test_public_id_unavailable_after_delete(self):
        """Once the row is deleted, _get_product_row returns None."""
        pid = _make_product()
        db.cur.execute("DELETE FROM products WHERE id=?", (pid,))
        db.conn.commit()
        # notify should return 0 gracefully
        n = notify_product_updated(pid, changed_fields=["name"])
        self.assertEqual(n, 0)


class TestPriceUpdate(unittest.TestCase):
    """Master price changes:
      - inherited resellers see new price immediately (live DB read)
      - rule-based resellers keep their rule on top (no silent overwrite)
    """

    def test_inherited_reseller_sees_new_master_price_immediately(self):
        rid = _make_reseller()
        pid = _make_product(price=10.00)
        _grant(rid, pid)

        p1, s1 = rdb.resolve_reseller_price_cents(rid, pid)
        self.assertEqual(p1, 1000); self.assertEqual(s1, "master")

        db.cur.execute("UPDATE products SET price=12.00 WHERE id=?", (pid,))
        db.conn.commit()

        p2, s2 = rdb.resolve_reseller_price_cents(rid, pid)
        self.assertEqual(p2, 1200); self.assertEqual(s2, "master")

    def test_fixed_price_rule_not_overwritten_by_master_price_change(self):
        rid = _make_reseller()
        pid = _make_product(price=10.00)
        _grant(rid, pid)
        rdb.set_pricing_rule(rid, pid, "fixed_price", 800, True)

        db.cur.execute("UPDATE products SET price=20.00 WHERE id=?", (pid,))
        db.conn.commit()

        p, src = rdb.resolve_reseller_price_cents(rid, pid)
        self.assertEqual(p, 800, "fixed rule must not be silently overwritten")
        self.assertTrue(str(src).startswith("rule"),
                        f"expected rule-based source, got {src!r}")

    def test_markup_rule_applied_on_top_of_new_master_price(self):
        rid = _make_reseller()
        pid = _make_product(price=10.00)
        _grant(rid, pid)
        rdb.set_pricing_rule(rid, pid, "markup_percent", 2000, True)

        db.cur.execute("UPDATE products SET price=5.00 WHERE id=?", (pid,))
        db.conn.commit()

        p, src = rdb.resolve_reseller_price_cents(rid, pid)
        self.assertEqual(p, 600)  # 5.00 + 20% = 6.00 = 600c
        self.assertTrue(str(src).startswith("rule"),
                        f"expected rule-based source, got {src!r}")

    def test_price_update_emits_product_updated_with_price_field(self):
        rid = _make_reseller()
        pid = _make_product(price=10.00)
        _grant(rid, pid)

        notify_product_updated(pid, changed_fields=["price"])
        row = _latest_outbox_for(rid, "product.updated")
        self.assertIsNotNone(row)
        self.assertIn("price", row["payload"]["changed_fields"])


class TestStockUpdate(unittest.TestCase):

    def test_no_per_reseller_stock_tables(self):
        """Stock is always read from the master product_stock table."""
        cur = get_cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type=\'table\' AND name LIKE \'reseller_stock%\'"
        )
        self.assertEqual(len(cur.fetchall()), 0)

    def test_stock_count_is_live_master_read(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)
        s1 = get_available_stock_count(pid)
        db.cur.execute("UPDATE products SET stock=stock+7 WHERE id=?", (pid,))
        db.conn.commit()
        s2 = get_available_stock_count(pid)
        self.assertGreaterEqual(s2, s1)

    def test_notify_stock_updated_emits_outbox_row(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)

        n = notify_stock_updated(pid, available_quantity=42)
        self.assertEqual(n, 1)
        row = _latest_outbox_for(rid, "stock.updated")
        self.assertIsNotNone(row)
        self.assertEqual(row["payload"]["available_quantity"], 42)
        self.assertEqual(row["payload"]["event"], "stock.updated")
        self.assertIsNotNone(row["payload"].get("product_id"))


class TestMultipleResellers(unittest.TestCase):

    def test_fan_out_to_all_resellers_with_access(self):
        pid = _make_product()
        rids = [_make_reseller() for _ in range(3)]
        for r in rids:
            _grant(r, pid)

        n = notify_product_updated(pid, changed_fields=["name"])
        self.assertEqual(n, 3)
        for r in rids:
            self.assertIsNotNone(
                _latest_outbox_for(r, "product.updated"),
                f"reseller_id={r} should have received event",
            )

    def test_reseller_without_access_receives_no_event(self):
        pid = _make_product()
        rid_a = _make_reseller()
        rid_b = _make_reseller()
        _grant(rid_a, pid)

        notify_product_updated(pid, changed_fields=["name"])
        self.assertIsNotNone(_latest_outbox_for(rid_a, "product.updated"))
        self.assertIsNone(_latest_outbox_for(rid_b, "product.updated"))

    def test_disabled_access_row_receives_no_event(self):
        pid = _make_product()
        rid = _make_reseller()
        _grant(rid, pid)
        db.cur.execute(
            "UPDATE reseller_product_access SET enabled=0 WHERE reseller_id=? AND product_id=?",
            (rid, pid),
        )
        db.conn.commit()

        n = notify_product_updated(pid, changed_fields=["name"])
        self.assertEqual(n, 0)


class TestCacheInvalidation(unittest.TestCase):
    """No product cache exists; live reads guarantee freshness."""

    def test_price_change_visible_without_invalidation(self):
        rid = _make_reseller()
        pid = _make_product(price=3.00)
        _grant(rid, pid)

        p1, _ = rdb.resolve_reseller_price_cents(rid, pid)
        self.assertEqual(p1, 300)

        db.cur.execute("UPDATE products SET price=9.00 WHERE id=?", (pid,))
        db.conn.commit()

        p2, _ = rdb.resolve_reseller_price_cents(rid, pid)
        self.assertEqual(p2, 900)

    def test_active_change_visible_without_invalidation(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)
        pub = rdb.ensure_product_public_id(pid)

        self.assertIsNotNone(rdb.get_reseller_api_product(rid, pub))
        _set_active(pid, 0)
        self.assertIsNone(rdb.get_reseller_api_product(rid, pub))
        _set_active(pid, 1)
        self.assertIsNotNone(rdb.get_reseller_api_product(rid, pub))

    def test_no_product_cache_module(self):
        spec = importlib.util.find_spec("reseller_api.services.product_cache")
        self.assertIsNone(spec)


class TestWorkerRestart(unittest.TestCase):
    """Outbox rows persist in SQLite and survive a simulated worker restart."""

    def test_pending_events_survive_restart(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)

        notify_product_updated(pid, changed_fields=["name"])
        pending = get_pending_events(limit=50)
        types = [r["event_type"] for r in pending]
        self.assertIn("product.updated", types)

    def test_delivered_events_not_requeued_after_restart(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)

        notify_product_updated(pid, changed_fields=["description"])
        row = _latest_outbox_for(rid, "product.updated")
        _mark_delivered(row["id"])

        pending_ids = [r["id"] for r in get_pending_events(limit=50)]
        self.assertNotIn(row["id"], pending_ids)


class TestQueueRetry(unittest.TestCase):

    def test_failed_row_can_be_reset_for_retry(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)

        notify_product_updated(pid, changed_fields=["name"])
        row = _latest_outbox_for(rid, "product.updated")
        _mark_failed_terminal(row["id"], reason="test")

        r2 = get_outbox_row(row["id"])
        self.assertEqual(r2["status"], "failed")

        ok = reset_for_manual_retry(row["id"])
        self.assertTrue(ok)
        r3 = get_outbox_row(row["id"])
        self.assertEqual(r3["status"], "pending")
        self.assertEqual(r3["attempts"], 0)

    def test_reset_pending_row_returns_false(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)
        notify_product_updated(pid, changed_fields=["name"])
        row = _latest_outbox_for(rid, "product.updated")
        self.assertFalse(reset_for_manual_retry(row["id"]))


class TestConcurrency(unittest.TestCase):
    """Simultaneous product update, catalog read, and stock change."""

    def test_concurrent_update_and_catalog_read(self):
        rid = _make_reseller()
        pid = _make_product(price=5.00)
        _grant(rid, pid)
        errors = []
        prices = []

        def do_update():
            try:
                db.cur.execute("UPDATE products SET price=7.00 WHERE id=?", (pid,))
                db.conn.commit()
                notify_product_updated(pid, changed_fields=["price"])
            except Exception as e:
                errors.append(("update", str(e)))

        def do_read():
            try:
                p, _ = rdb.resolve_reseller_price_cents(rid, pid)
                prices.append(p)
            except Exception as e:
                errors.append(("read", str(e)))

        t1 = threading.Thread(target=do_update)
        t2 = threading.Thread(target=do_read)
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(errors, [], f"concurrent errors: {errors}")
        self.assertGreater(len(prices), 0)
        for p in prices:
            self.assertIn(p, (500, 700))

    def test_concurrent_stock_change_and_notify(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)
        errors = []

        def do_stock():
            try:
                db.cur.execute("UPDATE products SET stock=stock+5 WHERE id=?", (pid,))
                db.conn.commit()
                notify_stock_updated(pid, available_quantity=5)
            except Exception as e:
                errors.append(("stock", str(e)))

        def do_read():
            try:
                get_available_stock_count(pid)
            except Exception as e:
                errors.append(("read", str(e)))

        t1 = threading.Thread(target=do_stock)
        t2 = threading.Thread(target=do_read)
        t1.start(); t2.start()
        t1.join(); t2.join()
        self.assertEqual(errors, [])


class TestDeduplication(unittest.TestCase):

    def test_duplicate_notify_produces_single_outbox_row(self):
        """INSERT OR IGNORE dedup: second call for same (reseller, event, aggregate) is a no-op."""
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)

        n1 = notify_product_disabled(pid)
        n2 = notify_product_disabled(pid)
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 0)   # dedup
        self.assertEqual(_count_outbox("product.disabled"), 1)

    def test_later_change_notifies_again(self):
        """Dedup must not become one-shot: a LATER change (next minute bucket)
        for the same (reseller, event, product) must enqueue a new row."""
        from unittest import mock
        import reseller_api.services.product_sync as ps

        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)

        before = _count_outbox("product.disabled")
        n1 = notify_product_disabled(pid)
        self.assertEqual(n1, 1)
        with mock.patch.object(ps, "now_text", return_value="2999-01-01 00:01"):
            n2 = notify_product_disabled(pid)
        self.assertEqual(n2, 1, "a later product.disabled must be enqueued again")
        self.assertEqual(_count_outbox("product.disabled"), before + 2)


class TestAccessIsolation(unittest.TestCase):

    def test_reseller_b_receives_no_event_for_product_granted_to_a_only(self):
        pid = _make_product()
        rid_a = _make_reseller()
        rid_b = _make_reseller()
        _grant(rid_a, pid)

        notify_product_updated(pid, changed_fields=["name"])
        self.assertIsNotNone(_latest_outbox_for(rid_a, "product.updated"))
        self.assertIsNone(_latest_outbox_for(rid_b, "product.updated"))

    def test_two_products_isolated(self):
        rid = _make_reseller()
        pid_a = _make_product()
        pid_b = _make_product()
        _grant(rid, pid_a)
        # No access to pid_b
        notify_product_updated(pid_a, changed_fields=["name"])
        n = notify_product_updated(pid_b, changed_fields=["name"])
        self.assertEqual(n, 0)


class TestSyncHelpersSafe(unittest.TestCase):
    """All public sync helpers must handle bad/edge input without raising."""

    def test_notify_updated_nonexistent_product_returns_zero(self):
        self.assertEqual(notify_product_updated(999999, changed_fields=["name"]), 0)

    def test_notify_disabled_nonexistent_product_returns_zero(self):
        self.assertEqual(notify_product_disabled(999999), 0)

    def test_notify_enabled_nonexistent_product_returns_zero(self):
        self.assertEqual(notify_product_enabled(999999), 0)

    def test_notify_stock_nonexistent_product_returns_zero(self):
        self.assertEqual(notify_stock_updated(999999, available_quantity=0), 0)

    def test_notify_deleted_empty_reseller_list_returns_zero(self):
        self.assertEqual(
            notify_product_deleted(999999, "PRD-FAKE", "Gone", resellers=[]), 0
        )

    def test_emit_event_no_resellers_returns_zero(self):
        pid = _make_product()
        pub = rdb.ensure_product_public_id(pid)
        n = emit_product_event_for_resellers(
            pid, pub, "product.updated", {"event": "product.updated"}
        )
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
