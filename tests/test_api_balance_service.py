import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
fd, path = tempfile.mkstemp(prefix="oggy_balance_api_", suffix=".db")
os.close(fd)
import config
config.DB_PATH = path
import database.db as db
db.DB_PATH = path
db._tls = threading.local()
db.init_db()
import database.reseller_db as rdb
from reseller_api.context import ApiRequestContext
from reseller_api.services.authentication import authenticate_credentials
from reseller_api.services.wallet import WalletError, credit_wallet, get_authenticated_balance


class BalanceApiServiceTests(unittest.TestCase):
    counter = 0

    def make_reseller(self, amount="0"):
        type(self).counter += 1
        n = self.counter
        uid = 996000 + n
        db.cur.execute("INSERT INTO users(user_id,first_name,username,blocked) VALUES(?,?,?,0)", (uid, "Balance", f"balance_{n}"))
        db.conn.commit()
        reseller = rdb.create_reseller(f"Balance {n}", uid, f"balance_{n}")
        rdb.set_reseller_status(reseller["id"], "active")
        key_id, secret = rdb.create_api_key(reseller["id"], "test")
        if amount != "0":
            credit_wallet(reseller["id"], amount, f"balance:fund-{n}")
        return reseller, uid, key_id, secret

    def context(self, key_id, secret):
        principal = authenticate_credentials(key_id, secret, "test")
        if not principal:
            return None
        return ApiRequestContext("a" * 32, principal.environment, principal.reseller_id, principal.reseller_public_id, principal.api_key_id, principal.api_key_public_id)

    def test_valid_reseller_reads_wallet_service_balance(self):
        reseller, _, key, secret = self.make_reseller("12.34")
        result = get_authenticated_balance(self.context(key, secret), "test")
        self.assertEqual(result, {"balance_cents": 1234, "balance": "12.34", "currency": "USDT"})

    def test_invalid_and_revoked_keys_fail_authentication(self):
        reseller, _, key, secret = self.make_reseller()
        self.assertIsNone(authenticate_credentials(key, "wrong", "test"))
        rdb.revoke_api_key_for_reseller(reseller["id"], key)
        self.assertIsNone(authenticate_credentials(key, secret, "test"))

    def test_suspended_and_blocked_resellers_fail_authentication(self):
        reseller, uid, key, secret = self.make_reseller()
        rdb.set_reseller_status(reseller["id"], "suspended")
        self.assertIsNone(authenticate_credentials(key, secret, "test"))
        rdb.set_reseller_status(reseller["id"], "active")
        db.cur.execute("UPDATE users SET blocked=1 WHERE user_id=?", (uid,)); db.conn.commit()
        self.assertIsNone(authenticate_credentials(key, secret, "test"))

    def test_context_cannot_be_changed_to_another_reseller(self):
        first, _, key, secret = self.make_reseller("10")
        second, _, _, _ = self.make_reseller("99")
        context = self.context(key, secret)
        self.assertEqual(get_authenticated_balance(context, "test")["balance"], "10.00")
        forged = ApiRequestContext(context.request_id, context.environment, second["id"], second["public_id"], context.api_key_id, context.api_key_public_id)
        with self.assertRaises(WalletError) as caught:
            get_authenticated_balance(forged, "test")
        self.assertEqual(caught.exception.code, "authorization_denied")

    @classmethod
    def tearDownClass(cls):
        db.close_thread_connection()
        try: os.unlink(path)
        except FileNotFoundError: pass


if __name__ == "__main__": unittest.main()
