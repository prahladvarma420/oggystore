import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
fd, path = tempfile.mkstemp(prefix="oggy_balance_http_", suffix=".db"); os.close(fd)
import config
config.DB_PATH = path
import database.db as db
db.DB_PATH = path; db._tls = threading.local(); db.init_db()
import database.reseller_db as rdb
from reseller_api import create_app
from reseller_api.services.wallet import credit_wallet
from reseller_api.services.rate_limit import RequestRateDecision


class ApiV1BalanceTests(unittest.TestCase):
    counter = 0
    @classmethod
    def setUpClass(cls):
        cls.patchers = [
            patch("reseller_api.middleware.request_context.persist_audit_record", return_value=True),
            patch("reseller_api.middleware.rate_limit.evaluate_request_rate", return_value=RequestRateDecision(True, "allowed", 60, 59, 45)),
            patch("reseller_api.middleware.security_controls.is_global_api_enabled", return_value=True),
        ]
        for item in cls.patchers: item.start()
        cls.app = create_app({"TESTING": True, "API_ENVIRONMENT": "test", "INIT_DATABASE": False, "RESELLER_API_HSTS": False})
        cls.client = cls.app.test_client()
    @classmethod
    def tearDownClass(cls):
        for item in reversed(cls.patchers): item.stop()
        db.close_thread_connection()
        try: os.unlink(path)
        except FileNotFoundError: pass
    def setUp(self):
        type(self).counter += 1; n = self.counter; self.uid = 997000 + n
        db.cur.execute("INSERT INTO users(user_id,first_name,username,blocked) VALUES(?,?,?,0)", (self.uid, "Balance HTTP", f"balance_http_{n}")); db.conn.commit()
        self.reseller = rdb.create_reseller(f"Balance HTTP {n}", self.uid, f"balance_http_{n}")
        rdb.set_reseller_status(self.reseller["id"], "active")
        self.key, self.secret = rdb.create_api_key(self.reseller["id"], "test")
        credit_wallet(self.reseller["id"], "42.50", f"http:fund-{n}")
    def headers(self, key=None, secret=None):
        return {"Authorization": f"Bearer {key or self.key}.{self.secret if secret is None else secret}"}
    def get(self, query="", headers=None): return self.client.get("/api/v1/balance" + query, headers=headers or self.headers())
    def test_valid_reseller_safe_response(self):
        response = self.get(); self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"], {"balance": "42.50", "currency": "USDT"})
        self.assertNotIn("reseller_id", response.get_data(as_text=True)); self.assertNotIn("transactions", response.get_data(as_text=True))
    def test_invalid_key(self): self.assertEqual(self.get(headers=self.headers(secret="wrong")).status_code, 401)
    def test_revoked_key(self):
        rdb.revoke_api_key_for_reseller(self.reseller["id"], self.key); self.assertEqual(self.get().status_code, 401)
    def test_suspended_reseller(self):
        rdb.set_reseller_status(self.reseller["id"], "suspended"); self.assertEqual(self.get().status_code, 401)
    def test_blocked_reseller(self):
        db.cur.execute("UPDATE users SET blocked=1 WHERE user_id=?", (self.uid,)); db.conn.commit(); self.assertEqual(self.get().status_code, 401)
    def test_reseller_id_manipulation_rejected(self):
        self.assertEqual(self.get("?reseller_id=1").status_code, 400)
    def test_other_reseller_wallet_cannot_be_selected(self):
        uid = self.uid + 10000
        db.cur.execute("INSERT INTO users(user_id,first_name,username,blocked) VALUES(?,?,?,0)", (uid, "Other", f"other_{uid}")); db.conn.commit()
        other = rdb.create_reseller("Other", uid, f"other_{uid}"); rdb.set_reseller_status(other["id"], "active"); credit_wallet(other["id"], "999", f"other:fund-{uid}")
        response = self.get(); self.assertEqual(response.get_json()["data"]["balance"], "42.50")
        self.assertEqual(self.get(f"?reseller_id={other['id']}").status_code, 400)


if __name__ == "__main__": unittest.main()
