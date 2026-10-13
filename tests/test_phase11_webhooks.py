"""
Phase 11 Prompt 8 -- Reseller product sync events & webhook integration.

Architecture reused from Phase 9 (no new webhook infrastructure created):
  - webhook_outbox.py  : transactional outbox, OUTBOX_EVENT_TYPES, payload builders
  - product_sync.py    : fan-out helpers (notify_product_*)
  - webhook_delivery.py: get_active_subscriptions, deliver_to_webhook, HMAC signing
  - webhook_worker.py  : _mark_delivered, _mark_failed_terminal, reset_for_manual_retry
  - reseller_db.py     : create_webhook, set_webhook_enabled_for_reseller

Events implemented (P6 audit confirmed in codebase):
  product.updated   product.disabled  product.enabled
  product.deleted   stock.updated

Events NOT implemented (no lifecycle code exists in codebase):
  product.created         (resellers don't auto-receive access)
  product.archived        (no archive concept)
  product.out_of_stock    (covered by stock.updated with quantity=0)
  product.back_in_stock   (covered by stock.updated with quantity>0)
  product.price_updated   (covered by product.updated with changed_fields=["price"])
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --- Isolated DB setup ---------------------------------------------------
_fd, _DB = tempfile.mkstemp(prefix="oggy_p11_wh_", suffix=".db")
os.close(_fd)

import config
config.DB_PATH = _DB
import os as _os; _os.environ.setdefault("DATA_ENCRYPTION_KEY", "dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdA==")
import database.db as db
db.DB_PATH = _DB
db._tls = threading.local()
db.init_db()

import database.reseller_db as rdb
from database.db import get_cursor, get_conn
from reseller_api.services.authentication import authenticate_credentials
from reseller_api.services.webhook_outbox import (
    OUTBOX_EVENT_TYPES,
    emit_event_atomic,
    get_outbox_row,
    get_pending_events,
    build_product_updated_payload,
    build_product_disabled_payload,
    build_product_enabled_payload,
    build_product_deleted_payload,
    build_stock_updated_payload,
)
from reseller_api.services.product_sync import (
    notify_product_updated,
    notify_product_disabled,
    notify_product_enabled,
    notify_product_deleted,
    notify_stock_updated,
    get_resellers_with_product_access,
)
from reseller_api.services.webhook_delivery import (
    get_active_subscriptions,
    get_webhook_secret_by_id,
    get_delivery_log_for_event,
    get_attempt_count,
    ensure_delivery_log_table,
)
from reseller_api.workers.webhook_worker import (
    _mark_delivered,
    _mark_failed_terminal,
    reset_for_manual_retry,
)
from reseller_api.services.webhook_signature import sign_body, verify_signature
from utils.timezone import now_text

ensure_delivery_log_table()

# -------------------------------------------------------------------------
_ctr = 0

def _uid():
    global _ctr
    _ctr += 1
    return 6_000_000 + _ctr

def _make_reseller():
    uid = _uid()
    n = _ctr
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"WHTest{n}", f"wh_{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"WHReseller {n}", uid, f"wh_user_{n}")
    rdb.set_reseller_status(rs["id"], "active")
    return rs["id"]

def _make_product(name="WH Widget", price=9.99, active=1, stock=0):
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
    rdb.set_product_access(reseller_id, product_id, enabled=True)

def _pub(product_id):
    return rdb.ensure_product_public_id(product_id)

def _name(product_id):
    db.cur.execute("SELECT name FROM products WHERE id=?", (product_id,))
    row = db.cur.fetchone()
    return row[0] if row else ""

def _set_active(product_id, val):
    db.cur.execute("UPDATE products SET active=? WHERE id=?", (int(val), product_id))
    db.conn.commit()

def _make_webhook(reseller_id, url="https://example.com/wh"):
    """Create a webhook and return (webhook_id, secret)."""
    row, secret = rdb.create_webhook(reseller_id, url)
    return row["id"], secret

def _payload(row_id):
    row = get_outbox_row(row_id)
    return json.loads(row["payload_json"]) if row else {}

def _pending_ids_for_reseller(reseller_id):
    """Return outbox row IDs in pending state for a given reseller."""
    pending = get_pending_events(limit=200)
    return [r["id"] for r in pending if r["reseller_id"] == reseller_id]


# =========================================================================

class TestImplementedEventTypes(unittest.TestCase):
    """Only events that have real lifecycle code are in the whitelist."""

    def test_product_updated_in_whitelist(self):
        self.assertIn("product.updated", OUTBOX_EVENT_TYPES)

    def test_product_disabled_in_whitelist(self):
        self.assertIn("product.disabled", OUTBOX_EVENT_TYPES)

    def test_product_enabled_in_whitelist(self):
        self.assertIn("product.enabled", OUTBOX_EVENT_TYPES)

    def test_product_deleted_in_whitelist(self):
        self.assertIn("product.deleted", OUTBOX_EVENT_TYPES)

    def test_stock_updated_in_whitelist(self):
        self.assertIn("stock.updated", OUTBOX_EVENT_TYPES)

    def test_product_created_not_in_whitelist(self):
        """product.created is not implemented (resellers don\'t auto-receive access)."""
        self.assertNotIn("product.created", OUTBOX_EVENT_TYPES)

    def test_product_archived_not_in_whitelist(self):
        """No archive concept in the codebase."""
        self.assertNotIn("product.archived", OUTBOX_EVENT_TYPES)

    def test_unknown_type_rejected_at_write_time(self):
        from reseller_api.services.webhook_outbox import emit_event
        import database.pgcompat as _pgcompat
        c, co = get_cursor(), get_conn()
        with self.assertRaises(ValueError):
            with _pgcompat.atomic(co):
                emit_event(c, 1, "product.archived", "PRD-X", {})


class TestProductEnabledEvent(unittest.TestCase):

    def test_notify_enabled_emits_outbox_row(self):
        rid = _make_reseller()
        pid = _make_product(active=1)
        _grant(rid, pid)

        n = notify_product_enabled(pid)
        self.assertGreaterEqual(n, 1, "at least 1 outbox row for reseller with access")

    def test_enabled_payload_has_active_true(self):
        pub = "PRD-ENABLED-TEST"
        payload = build_product_enabled_payload(pub, "Test Product", now_text())
        self.assertTrue(payload["active"])
        self.assertEqual(payload["event_type"], "product.enabled")
        self.assertEqual(payload["product_id"], pub)

    def test_enabled_payload_uses_public_id_not_internal(self):
        pub = "PRD-EXT-1"
        payload = build_product_enabled_payload(pub, "Test", now_text())
        self.assertNotIn("internal_id", payload)
        self.assertNotIn("id", payload)
        self.assertEqual(payload["product_id"], pub)

    def test_enabled_payload_excludes_secret_fields(self):
        payload = build_product_enabled_payload("PRD-1", "Widget", now_text())
        for bad_key in ("cost", "redeem_link", "delivery", "rules",
                        "secret", "credentials", "admin_notes"):
            self.assertNotIn(bad_key, payload)

    def test_no_resellers_with_access_emits_zero(self):
        pid = _make_product(active=1)
        # no _grant
        n = notify_product_enabled(pid)
        self.assertEqual(n, 0)


class TestProductDisabledEvent(unittest.TestCase):

    def test_notify_disabled_emits_outbox_row(self):
        rid = _make_reseller()
        pid = _make_product(active=1)
        _grant(rid, pid)
        n = notify_product_disabled(pid)
        self.assertGreaterEqual(n, 1)

    def test_disabled_payload_has_active_false(self):
        pub = "PRD-DIS-1"
        payload = build_product_disabled_payload(pub, "Widget", now_text())
        self.assertFalse(payload["active"])
        self.assertEqual(payload["event_type"], "product.disabled")

    def test_disabled_payload_excludes_secrets(self):
        payload = build_product_disabled_payload("PRD-1", "Widget", now_text())
        for bad_key in ("cost", "delivery", "rules", "secret", "supplier"):
            self.assertNotIn(bad_key, payload)

    def test_disabled_outbox_row_is_pending(self):
        rid = _make_reseller()
        pid = _make_product(active=1)
        _grant(rid, pid)
        notify_product_disabled(pid)
        pending = _pending_ids_for_reseller(rid)
        self.assertGreater(len(pending), 0)
        row = get_outbox_row(pending[-1])
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["event_type"], "product.disabled")


class TestProductUpdatedEvent(unittest.TestCase):

    def test_notify_updated_emits_outbox_row(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)
        n = notify_product_updated(pid, changed_fields=["name", "price"])
        self.assertGreaterEqual(n, 1)

    def test_updated_payload_includes_changed_fields(self):
        payload = build_product_updated_payload(
            "PRD-UPD-1", "Widget", ["name", "price"], now_text()
        )
        self.assertIn("changed_fields", payload)
        self.assertIn("name", payload["changed_fields"])
        self.assertIn("price", payload["changed_fields"])
        self.assertEqual(payload["event_type"], "product.updated")

    def test_price_update_uses_product_updated_event(self):
        """price.updated is represented as product.updated with changed_fields=[price]."""
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)
        n = notify_product_updated(pid, changed_fields=["price"])
        self.assertGreaterEqual(n, 1)
        pending = _pending_ids_for_reseller(rid)
        row = get_outbox_row(pending[-1])
        p = json.loads(row["payload_json"])
        self.assertEqual(row["event_type"], "product.updated")
        self.assertIn("price", p.get("changed_fields", []))

    def test_updated_payload_excludes_internal_cost(self):
        payload = build_product_updated_payload("PRD-1", "X", [], now_text())
        for bad in ("cost", "redeem_link", "delivery", "rules",
                    "secret", "credentials"):
            self.assertNotIn(bad, payload)


class TestProductUnavailableRestored(unittest.TestCase):
    """product.out_of_stock and product.back_in_stock use stock.updated."""

    def test_stock_zero_emits_stock_updated_event(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)
        n = notify_stock_updated(pid, available_quantity=0)
        self.assertGreaterEqual(n, 1)
        pending = _pending_ids_for_reseller(rid)
        row = get_outbox_row(pending[-1])
        self.assertEqual(row["event_type"], "stock.updated")
        p = json.loads(row["payload_json"])
        self.assertEqual(p["available_quantity"], 0)

    def test_stock_restored_emits_stock_updated_event(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)
        n = notify_stock_updated(pid, available_quantity=50)
        self.assertGreaterEqual(n, 1)
        pending = _pending_ids_for_reseller(rid)
        row = get_outbox_row(pending[-1])
        self.assertEqual(row["event_type"], "stock.updated")
        p = json.loads(row["payload_json"])
        self.assertEqual(p["available_quantity"], 50)

    def test_stock_payload_excludes_codes_and_secrets(self):
        payload = build_stock_updated_payload("PRD-1", "Widget", 10, now_text())
        for bad in ("stock_ids", "delivery", "codes", "items",
                    "secret", "cost", "credentials"):
            self.assertNotIn(bad, payload)
        self.assertIn("available_quantity", payload)


class TestResellerMapping(unittest.TestCase):
    """Only resellers with product access receive events."""

    def test_reseller_with_access_receives_event(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)

        n = notify_product_updated(pid)
        self.assertGreaterEqual(n, 1)

        targets = get_resellers_with_product_access(pid)
        self.assertTrue(any(t["reseller_id"] == rid for t in targets))

    def test_reseller_without_access_not_in_targets(self):
        rid_with = _make_reseller()
        rid_without = _make_reseller()
        pid = _make_product()
        _grant(rid_with, pid)
        # rid_without gets no grant

        targets = get_resellers_with_product_access(pid)
        ids = [t["reseller_id"] for t in targets]
        self.assertIn(rid_with, ids)
        self.assertNotIn(rid_without, ids)

    def test_disabled_access_row_not_in_targets(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)
        rdb.set_product_access(rid, pid, enabled=False)

        targets = get_resellers_with_product_access(pid)
        ids = [t["reseller_id"] for t in targets]
        self.assertNotIn(rid, ids)


class TestUnrelatedReseller(unittest.TestCase):
    """A reseller with no access to the changed product receives no event."""

    def test_notify_does_not_emit_for_reseller_without_access(self):
        rid_related = _make_reseller()
        rid_other = _make_reseller()
        pid = _make_product()
        _grant(rid_related, pid)
        # rid_other has access to a DIFFERENT product
        pid2 = _make_product(name="Other Product")
        _grant(rid_other, pid2)

        before_other = len(_pending_ids_for_reseller(rid_other))
        notify_product_updated(pid)  # only pid changed
        after_other = len(_pending_ids_for_reseller(rid_other))

        self.assertEqual(before_other, after_other,
                         "rid_other must not receive event for a product it has no access to")

    def test_emit_targets_specific_reseller_id(self):
        rid_a = _make_reseller()
        rid_b = _make_reseller()
        pid = _make_product()
        _grant(rid_a, pid)  # only A gets access

        before_b = len(_pending_ids_for_reseller(rid_b))
        notify_product_disabled(pid)
        after_b = len(_pending_ids_for_reseller(rid_b))
        self.assertEqual(before_b, after_b)

        after_a = len(_pending_ids_for_reseller(rid_a))
        self.assertGreater(after_a, 0)


class TestMultipleResellers(unittest.TestCase):
    """All resellers with access receive exactly one outbox row each."""

    def test_three_resellers_all_receive_event(self):
        rids = [_make_reseller() for _ in range(3)]
        pid = _make_product(name="Multi Widget")
        for r in rids:
            _grant(r, pid)

        n = notify_product_updated(pid, changed_fields=["name"])
        self.assertEqual(n, 3)

    def test_five_resellers_correct_fan_out(self):
        rids = [_make_reseller() for _ in range(5)]
        pid = _make_product()
        for r in rids:
            _grant(r, pid)

        n = notify_product_disabled(pid)
        self.assertEqual(n, 5)

    def test_one_reseller_without_access_not_counted(self):
        rids = [_make_reseller() for _ in range(3)]
        rid_out = _make_reseller()
        pid = _make_product()
        for r in rids:
            _grant(r, pid)
        # rid_out has no grant

        n = notify_product_enabled(pid)
        self.assertEqual(n, 3)


class TestWebhookSubscription(unittest.TestCase):
    """get_active_subscriptions correctly filters by reseller and event type."""

    def test_enabled_webhook_appears_in_subscriptions(self):
        rid = _make_reseller()
        wid, _ = _make_webhook(rid, "https://hooks.example.com/p8")
        subs = get_active_subscriptions(rid, "product.disabled")
        ids = [s["id"] for s in subs]
        self.assertIn(wid, ids)

    def test_disabled_webhook_not_in_subscriptions(self):
        rid = _make_reseller()
        wid, _ = _make_webhook(rid, "https://hooks.example.com/dis-p8")
        rdb.set_webhook_enabled_for_reseller(rid, wid, False)
        subs = get_active_subscriptions(rid, "product.updated")
        ids = [s["id"] for s in subs]
        self.assertNotIn(wid, ids)

    def test_different_reseller_webhook_not_returned(self):
        rid_a = _make_reseller()
        rid_b = _make_reseller()
        wid_a, _ = _make_webhook(rid_a, "https://hooks.example.com/ra-p8")
        subs = get_active_subscriptions(rid_b, "product.updated")
        ids = [s["id"] for s in subs]
        self.assertNotIn(wid_a, ids)


class TestHmacSigning(unittest.TestCase):
    """HMAC-SHA256 signing and verification for webhook payloads."""

    def test_sign_and_verify_round_trip(self):
        secret = "test-secret-abc-12345"
        body = json.dumps({"event_type": "product.updated", "product_id": "PRD-1"}).encode()
        timestamp = now_text()
        signature = sign_body(secret, body, timestamp)
        self.assertTrue(
            verify_signature(secret, body, timestamp, signature, check_freshness=False),
            "signed payload must verify correctly"
        )

    def test_tampered_body_fails_verification(self):
        secret = "test-secret-xyz"
        body = json.dumps({"event_type": "product.enabled"}).encode()
        timestamp = now_text()
        sig = sign_body(secret, body, timestamp)
        tampered = json.dumps({"event_type": "product.deleted"}).encode()
        self.assertFalse(verify_signature(secret, tampered, timestamp, sig, check_freshness=False))

    def test_wrong_secret_fails_verification(self):
        body = json.dumps({"event": "product.disabled"}).encode()
        timestamp = now_text()
        sig = sign_body("correct-secret", body, timestamp)
        self.assertFalse(verify_signature("wrong-secret", body, timestamp, sig, check_freshness=False))

    def test_signature_format_is_sha256_prefix(self):
        secret = "any-secret"
        body = b"hello"
        sig = sign_body(secret, body, now_text())
        self.assertTrue(sig.startswith("sha256="), f"signature must start with sha256=, got: {sig!r}")


class TestWebhookDeliveryMocked(unittest.TestCase):
    """Test delivery, retry, and dead-letter using mocked HTTP."""

    def _emit_one(self, reseller_id, event_type="product.updated"):
        """Emit a single outbox row and return its id."""
        from reseller_api.services.webhook_outbox import emit_event_atomic
        pub = f"PRD-MOCK-{_uid()}"
        row_id = emit_event_atomic(
            reseller_id, event_type, pub,
            {"product_id": pub, "event_type": event_type, "timestamp": now_text()},
        )
        return row_id

    def test_mark_delivered_sets_status(self):
        rid = _make_reseller()
        row_id = self._emit_one(rid)
        self.assertIsNotNone(row_id)
        _mark_delivered(row_id)
        row = get_outbox_row(row_id)
        self.assertEqual(row["status"], "delivered")

    def test_mark_failed_terminal_sets_status(self):
        rid = _make_reseller()
        row_id = self._emit_one(rid)
        _mark_failed_terminal(row_id, reason="test terminal failure")
        row = get_outbox_row(row_id)
        self.assertEqual(row["status"], "failed")

    def test_reset_for_manual_retry_restores_pending(self):
        rid = _make_reseller()
        row_id = self._emit_one(rid)
        _mark_failed_terminal(row_id, reason="terminal")
        ok = reset_for_manual_retry(row_id)
        self.assertTrue(ok)
        row = get_outbox_row(row_id)
        self.assertEqual(row["status"], "pending")

    def test_reset_pending_row_returns_false(self):
        rid = _make_reseller()
        row_id = self._emit_one(rid)
        # Row is still pending — reset should be a no-op
        ok = reset_for_manual_retry(row_id)
        self.assertFalse(ok)

    def test_delivered_event_not_in_pending_list(self):
        rid = _make_reseller()
        row_id = self._emit_one(rid)
        _mark_delivered(row_id)
        pending = get_pending_events(limit=200)
        ids = [r["id"] for r in pending]
        self.assertNotIn(row_id, ids)

    def test_deliver_to_webhook_mocked_success(self):
        """Successful HTTP delivery marks the event delivered and logs attempt."""
        from reseller_api.services.webhook_delivery import deliver_to_webhook
        rid = _make_reseller()
        wid, secret = _make_webhook(rid, "https://p8.example.com/hooks")
        row_id = self._emit_one(rid, "product.disabled")
        row = get_outbox_row(row_id)

        with patch("reseller_api.services.webhook_delivery._send_http") as mock_http:
            mock_http.return_value = {
                "success": True, "http_status": 200,
                "error_category": None, "responded_at": now_text(),
                "elapsed_ms": 42,
            }
            result = deliver_to_webhook(
                outbox_event_id=row_id,
                outbox_row=row,
                webhook_id=wid,
                webhook_url="https://p8.example.com/hooks",
                reseller_id=rid,
                attempt=1,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["http_status"], 200)
        log = get_delivery_log_for_event(row_id)
        self.assertGreater(len(log), 0)

    def test_deliver_to_webhook_mocked_failure(self):
        """Failed HTTP delivery is logged with error_category."""
        from reseller_api.services.webhook_delivery import deliver_to_webhook
        rid = _make_reseller()
        wid, _ = _make_webhook(rid, "https://p8fail.example.com/hooks")
        row_id = self._emit_one(rid, "product.enabled")
        row = get_outbox_row(row_id)

        with patch("reseller_api.services.webhook_delivery._send_http") as mock_http:
            mock_http.return_value = {
                "success": False, "http_status": 500,
                "error_category": "5xx", "responded_at": now_text(),
                "elapsed_ms": 100,
            }
            result = deliver_to_webhook(
                outbox_event_id=row_id,
                outbox_row=row,
                webhook_id=wid,
                webhook_url="https://p8fail.example.com/hooks",
                reseller_id=rid,
                attempt=1,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["http_status"], 500)


class TestDuplicateEvent(unittest.TestCase):
    """Duplicate product sync calls produce no duplicate outbox rows."""

    def test_repeated_notify_does_not_duplicate_rows(self):
        rid = _make_reseller()
        pid = _make_product()
        _grant(rid, pid)

        n1 = notify_product_disabled(pid)
        pending_before = _pending_ids_for_reseller(rid)

        n2 = notify_product_disabled(pid)
        pending_after = _pending_ids_for_reseller(rid)

        # Second call should produce 0 new rows (INSERT OR IGNORE dedup)
        self.assertEqual(n2, 0,
                         "duplicate notification for same product+event must be deduped")
        self.assertEqual(len(pending_after), len(pending_before))

    def test_event_id_stable_across_retries(self):
        """idempotency_key injected by deliver_to_webhook equals event_id."""
        # Verified by checking deliver_to_webhook source adds event_id as idempotency_key
        from reseller_api.services.webhook_delivery import deliver_to_webhook
        rid = _make_reseller()
        wid, _ = _make_webhook(rid, "https://idem.example.com/wh")

        from reseller_api.services.webhook_outbox import emit_event_atomic
        pub = f"PRD-IDEM-{_uid()}"
        row_id = emit_event_atomic(
            rid, "product.updated", pub,
            {"product_id": pub, "event_type": "product.updated", "timestamp": now_text()},
        )
        row = get_outbox_row(row_id)

        captured = {}
        def _fake_http(url, secret, payload, event_type, request_id):
            captured.update(payload)
            return {"success": True, "http_status": 200,
                    "error_category": None, "responded_at": now_text(),
                    "elapsed_ms": 1}

        with patch("reseller_api.services.webhook_delivery._send_http", side_effect=_fake_http):
            deliver_to_webhook(
                outbox_event_id=row_id, outbox_row=row,
                webhook_id=wid, webhook_url="https://idem.example.com/wh",
                reseller_id=rid, attempt=1,
            )

        self.assertEqual(captured.get("idempotency_key"), str(row_id),
                         "idempotency_key must equal the stable outbox row id")


class TestPayloadSecurity(unittest.TestCase):
    """All product payloads exclude internal / admin-only fields."""

    _FORBIDDEN = (
        "cost", "redeem_link", "delivery", "rules",
        "secret", "credentials", "supplier", "admin_notes",
        "internal_id", "encrypted",
    )

    def _check_clean(self, payload):
        for bad in self._FORBIDDEN:
            self.assertNotIn(bad, payload, f"payload must not contain {bad!r}")
        # Must use public product_id string, not raw int
        self.assertIn("product_id", payload)
        self.assertIsInstance(payload["product_id"], str)

    def test_updated_payload_is_clean(self):
        self._check_clean(build_product_updated_payload("PRD-1", "X", [], now_text()))

    def test_disabled_payload_is_clean(self):
        self._check_clean(build_product_disabled_payload("PRD-1", "X", now_text()))

    def test_enabled_payload_is_clean(self):
        self._check_clean(build_product_enabled_payload("PRD-1", "X", now_text()))

    def test_deleted_payload_is_clean(self):
        self._check_clean(build_product_deleted_payload("PRD-1", "X", now_text()))

    def test_stock_payload_is_clean(self):
        self._check_clean(build_stock_updated_payload("PRD-1", "X", 5, now_text()))


if __name__ == "__main__":
    unittest.main()
