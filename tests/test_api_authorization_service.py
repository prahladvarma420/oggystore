import inspect
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_fd, _db_path = tempfile.mkstemp(prefix="oggy_api_authorization_", suffix=".db")
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
from reseller_api.services.authorization import (
    BALANCE_READ,
    FOUNDATION_READ,
    ORDERS_CREATE,
    PRODUCTS_READ,
    authorize_operation,
    authorize_product_access,
    can_access_product,
    can_create_order,
    can_perform_operation,
    is_api_enabled,
    is_reseller_blocked,
)


class ApiAuthorizationServiceTests(unittest.TestCase):
    counter = 0

    def setUp(self):
        type(self).counter += 1
        n = self.counter
        self.uid = 910000 + n
        db.cur.execute(
            "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
            (self.uid, "Authorization Test", f"authz_{n}"),
        )
        db.conn.commit()
        self.reseller = rdb.create_reseller(f"Authorization {n}", self.uid, f"authz_{n}")
        rdb.set_reseller_status(self.reseller["id"], "active")
        self.key_id, self.secret = rdb.create_api_key(self.reseller["id"], "test")
        principal = authenticate_credentials(self.key_id, self.secret, "test")
        self.assertIsNotNone(principal)
        self.context = ApiRequestContext(
            request_id=("%032x" % n),
            environment=principal.environment,
            reseller_id=principal.reseller_id,
            reseller_public_id=principal.reseller_public_id,
            api_key_id=principal.api_key_id,
            api_key_public_id=principal.api_key_public_id,
        )
        db.cur.execute(
            "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,?,?,1)",
            (f"Authorization Product {n}", 10.0, 1, "TEST-CODE"),
        )
        self.product_id = db.cur.lastrowid
        db.conn.commit()
        rdb.set_product_access(self.reseller["id"], self.product_id, True)

    @classmethod
    def tearDownClass(cls):
        try:
            db.close_thread_connection()
        finally:
            try:
                os.unlink(_db_path)
            except FileNotFoundError:
                pass

    def test_supported_operation_uses_authenticated_context_and_limits(self):
        decision = authorize_operation(self.context, FOUNDATION_READ, "test")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "authorized")
        self.assertEqual(decision.limits["rate_limit_per_min"], 60)
        self.assertTrue(can_perform_operation(self.context, BALANCE_READ, "test"))
        self.assertTrue(is_api_enabled(self.context, "test"))

    def test_product_access_is_reseller_scoped(self):
        self.assertTrue(can_access_product(self.context, self.product_id, "test"))
        rdb.set_product_access(self.reseller["id"], self.product_id, False)
        decision = authorize_product_access(self.context, self.product_id, "test")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "product_access_denied")

    def test_inactive_product_is_denied_even_with_access_grant(self):
        db.cur.execute("UPDATE products SET active=0 WHERE id=?", (self.product_id,))
        db.conn.commit()
        decision = authorize_product_access(self.context, self.product_id, "test")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "product_unavailable")

    def test_cross_reseller_product_access_is_denied(self):
        uid2 = self.uid + 1000000
        db.cur.execute(
            "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
            (uid2, "Other", f"other_{self.counter}"),
        )
        db.conn.commit()
        other = rdb.create_reseller(f"Other {self.counter}", uid2, f"other_{self.counter}")
        rdb.set_reseller_status(other["id"], "active")
        other_key, other_secret = rdb.create_api_key(other["id"], "test")
        principal = authenticate_credentials(other_key, other_secret, "test")
        other_context = ApiRequestContext(
            request_id="f" * 32,
            environment=principal.environment,
            reseller_id=principal.reseller_id,
            reseller_public_id=principal.reseller_public_id,
            api_key_id=principal.api_key_id,
            api_key_public_id=principal.api_key_public_id,
        )
        self.assertFalse(can_access_product(other_context, self.product_id, "test"))
        self.assertTrue(can_access_product(self.context, self.product_id, "test"))

    def test_mixed_key_and_reseller_context_is_idor_denied(self):
        uid2 = self.uid + 2000000
        db.cur.execute(
            "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
            (uid2, "Other", f"mixed_{self.counter}"),
        )
        db.conn.commit()
        other = rdb.create_reseller(f"Mixed {self.counter}", uid2, f"mixed_{self.counter}")
        rdb.set_reseller_status(other["id"], "active")
        other_key, _other_secret = rdb.create_api_key(other["id"], "test")
        other_record = rdb.get_api_auth_record(other_key)
        forged = ApiRequestContext(
            request_id="e" * 32,
            environment="test",
            reseller_id=self.context.reseller_id,
            reseller_public_id=self.context.reseller_public_id,
            api_key_id=other_record["api_key_id"],
            api_key_public_id=other_key,
        )
        decision = authorize_operation(forged, PRODUCTS_READ, "test")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "key_reseller_scope_mismatch")

    def test_client_cannot_override_reseller_price_or_balance(self):
        parameters = inspect.signature(authorize_product_access).parameters
        self.assertNotIn("reseller_id", parameters)
        self.assertNotIn("price", parameters)
        self.assertNotIn("balance", parameters)
        with self.assertRaises(TypeError):
            authorize_product_access(
                self.context,
                self.product_id,
                "test",
                reseller_id=999999,
            )
        self.assertTrue(can_access_product(self.context, self.product_id, "test"))

    def test_status_change_after_authentication_is_immediately_denied(self):
        rdb.set_reseller_status(self.reseller["id"], "suspended")
        self.assertFalse(can_perform_operation(self.context, FOUNDATION_READ, "test"))
        self.assertFalse(can_create_order(self.context, "test"))

    def test_block_change_after_authentication_is_immediately_denied(self):
        db.cur.execute("UPDATE users SET blocked=1 WHERE user_id=?", (self.uid,))
        db.conn.commit()
        self.assertTrue(is_reseller_blocked(self.context, "test"))
        self.assertFalse(can_perform_operation(self.context, FOUNDATION_READ, "test"))

    def test_key_disable_and_revocation_take_effect_immediately(self):
        rdb.set_api_key_enabled_for_reseller(self.reseller["id"], self.key_id, False)
        self.assertFalse(can_perform_operation(self.context, FOUNDATION_READ, "test"))
        rdb.set_api_key_enabled_for_reseller(self.reseller["id"], self.key_id, True)
        self.assertTrue(rdb.revoke_api_key_for_reseller(self.reseller["id"], self.key_id))
        self.assertFalse(can_perform_operation(self.context, FOUNDATION_READ, "test"))

    def test_environment_mismatch_is_denied(self):
        self.assertFalse(can_perform_operation(self.context, FOUNDATION_READ, "production"))
        forged = ApiRequestContext(
            request_id=self.context.request_id,
            environment="production",
            reseller_id=self.context.reseller_id,
            reseller_public_id=self.context.reseller_public_id,
            api_key_id=self.context.api_key_id,
            api_key_public_id=self.context.api_key_public_id,
        )
        self.assertFalse(can_perform_operation(forged, FOUNDATION_READ, "production"))

    def test_api_disabled_is_denied(self):
        rdb.set_reseller_limits(self.reseller["id"], api_enabled=0)
        self.assertFalse(is_api_enabled(self.context, "test"))
        self.assertFalse(can_perform_operation(self.context, FOUNDATION_READ, "test"))

    def test_order_permission_respects_configured_limits(self):
        self.assertTrue(can_create_order(self.context, "test"))
        rdb.set_reseller_limits(self.reseller["id"], orders_per_min=0)
        decision = authorize_operation(self.context, ORDERS_CREATE, "test")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "order_limit_disabled")
        self.assertTrue(can_perform_operation(self.context, PRODUCTS_READ, "test"))

    def test_request_limit_zero_denies_all_supported_operations(self):
        rdb.set_reseller_limits(self.reseller["id"], daily_request_limit=0)
        decision = authorize_operation(self.context, PRODUCTS_READ, "test")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "request_limit_disabled")

    def test_unknown_permission_is_deny_by_default(self):
        decision = authorize_operation(self.context, "admin:everything", "test")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "unsupported_operation")


if __name__ == "__main__":
    unittest.main(verbosity=2)
