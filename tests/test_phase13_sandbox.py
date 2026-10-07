"""Phase 13 Prompt 7 — sandbox / testing environment.

The sandbox IS the existing test/production environment split, hardened:

  * keys are environment-pinned in BOTH directions (test keys never work on
    production; production keys never work on the sandbox) — pre-existing
  * NEW: the database is stamped with its environment at first API boot;
    a mismatched API instance refuses to start (fail closed) — the
    "sandbox data never enters production" guarantee
  * NEW: POST /webhooks/<id>/test — a signed synchronous test.ping for
    webhook integration testing with zero order/stock/wallet side effects
"""

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "DATA_ENCRYPTION_KEY",
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
)

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase13_sandbox_", suffix=".db")
os.close(_fd)

import config
config.DB_PATH = _db_path
import database.db as db
db.DB_PATH = _db_path
db._tls = threading.local()
db.init_db()

import database.reseller_db as rdb
from reseller_api import create_app
import reseller_api.middleware.request_context  # noqa: F401
import reseller_api.middleware.rate_limit  # noqa: F401
import reseller_api.middleware.security_controls  # noqa: F401
from reseller_api.services.rate_limit import RequestRateDecision

_ctr = 0


def _n():
    global _ctr
    _ctr += 1
    return _ctr


def _make_reseller():
    n = _n()
    uid = 9_790_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P13S{n}", f"p13s{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P13S{n}", uid, f"p13s{n}")
    rdb.set_reseller_status(rs["id"], "active")
    return rs


class Phase13SandboxTests(unittest.TestCase):
    """The suite runs the API in TEST environment (the sandbox)."""

    @classmethod
    def setUpClass(cls):
        cls.p1 = patch(
            "reseller_api.middleware.request_context.persist_audit_record",
            return_value=True)
        cls.p1.start()
        cls.p2 = patch(
            "reseller_api.middleware.rate_limit.evaluate_request_rate",
            return_value=RequestRateDecision(
                True, "allowed", limit=60, remaining=59, reset_after=45))
        cls.p2.start()
        cls.p3 = patch(
            "reseller_api.middleware.security_controls.is_global_api_enabled",
            return_value=True)
        cls.p3.start()
        cls.app = create_app({
            "TESTING": True, "API_ENVIRONMENT": "test",
            "INIT_DATABASE": False, "RESELLER_API_HSTS": False,
        })
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.p3.stop()
        cls.p2.stop()
        cls.p1.stop()
        try:
            db.close_thread_connection()
        finally:
            try:
                os.unlink(_db_path)
            except FileNotFoundError:
                pass

    # ---------------------------------------------------------- env stamping

    def test_first_boot_stamps_environment(self):
        db.cur.execute("SELECT value FROM settings WHERE key='deployment_environment'")
        self.assertEqual(db.cur.fetchone()[0], "test")

    def test_mismatched_api_refuses_to_boot(self):
        """A production-configured API against this test-stamped DB must fail."""
        from reseller_api.config import validate_environment_marker
        with self.assertRaises(RuntimeError) as ctx:
            validate_environment_marker({"API_ENVIRONMENT": "production"})
        self.assertIn("test", str(ctx.exception))
        self.assertIn("production", str(ctx.exception))

    def test_matching_environment_boots(self):
        from reseller_api.config import validate_environment_marker
        validate_environment_marker({"API_ENVIRONMENT": "test"})  # no raise

    def test_production_stamped_db_rejects_test_api(self):
        """Symmetric: a fresh DB stamped production refuses a test API."""
        from database.db import set_setting
        from reseller_api.config import validate_environment_marker
        _fd2, other_db = tempfile.mkstemp(suffix=".db")
        os.close(_fd2)
        try:
            import database.db as dbmod
            # stamp directly via a throwaway connection
            import sqlite3
            c = sqlite3.connect(other_db)
            c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
            c.execute("INSERT INTO settings(key, value) VALUES('deployment_environment','production')")
            c.commit(); c.close()
            real = dbmod.DB_PATH
            # temporarily point the shared connection layer at the other DB
            dbmod.close_thread_connection()
            dbmod.DB_PATH = other_db
            dbmod._tls = threading.local()
            with self.assertRaises(RuntimeError):
                validate_environment_marker({"API_ENVIRONMENT": "test"})
        finally:
            dbmod.DB_PATH = real
            dbmod._tls = threading.local()
            os.unlink(other_db)

    # ---------------------------------------------------------- key isolation

    def test_test_and_production_keys_are_isolated_both_ways(self):
        rs = _make_reseller()
        test_id, test_secret = rdb.create_api_key(rs["id"], "test")
        live_id, live_secret = rdb.create_api_key(rs["id"], "production")

        # sandbox (this suite is env=test): test key works, live key does not
        ok = self.client.get("/api/v1/balance",
                             headers={"Authorization": f"Bearer {test_id}.{test_secret}"})
        self.assertEqual(ok.status_code, 200)
        no = self.client.get("/api/v1/balance",
                             headers={"Authorization": f"Bearer {live_id}.{live_secret}"})
        self.assertEqual(no.status_code, 401)
        self.assertEqual(no.get_json()["error"]["code"], "authentication_failed")

        # and the service layer agrees (env pinned in both directions)
        from reseller_api.services.authentication import authenticate_credentials
        self.assertIsNone(authenticate_credentials(live_id, live_secret, "test"))
        self.assertIsNone(authenticate_credentials(test_id, test_secret, "production"))
        self.assertIsNotNone(authenticate_credentials(live_id, live_secret, "production"))

    # ---------------------------------------------------------- sandbox flows are real-but-safe

    def test_sandbox_order_full_fidelity(self):
        """In the sandbox the whole pipeline runs for real — validation,
        wallet, stock, order, delivery — against sandbox-only data."""
        rs = _make_reseller()
        key, sec = rdb.create_api_key(rs["id"], "test")
        n = _n()
        db.cur.execute(
            "INSERT INTO products(name, price, stock, delivery, active)"
            " VALUES(?,0.01,0,'code_stock',1)", (f"TEST-Product {n}",))
        db.conn.commit()
        pid = db.cur.lastrowid
        db.cur.execute(
            "INSERT INTO product_stock(product_id, delivery, sold) VALUES(?,'TEST-CODE-1',0)",
            (pid,))
        db.conn.commit()
        rdb.set_product_access(rs["id"], pid, True)
        rdb.import_reseller_product(rs["id"], pid)
        pub = rdb.ensure_product_public_id(pid)
        rdb.post_wallet_transaction(rs["id"], "topup_credit", 500, "TP-SB-1", "seed")

        r = self.client.post("/api/v1/orders",
                             headers={"Authorization": f"Bearer {key}.{sec}"},
                             json={"product_id": pub, "quantity": 1,
                                   "client_order_id": "sandbox-1"})
        self.assertEqual(r.status_code, 201, r.get_json())
        oid = r.get_json()["data"]["order_id"]
        dv = self.client.get(f"/api/v1/orders/{oid}/delivery",
                             headers={"Authorization": f"Bearer {key}.{sec}"})
        self.assertEqual(dv.get_json()["data"]["delivery"]["items"], ["TEST-CODE-1"])

    # ---------------------------------------------------------- webhook ping

    def test_webhook_ping_signed_and_synchronous(self):
        rs = _make_reseller()
        key, sec = rdb.create_api_key(rs["id"], "test")
        hook, hook_secret = rdb.create_webhook(rs["id"], "https://sb.example/hook")
        whk = f"WHK-{hook['id']:08d}"

        captured = {}

        from reseller_api.services import webhook_delivery as wd
        real_send = wd._send_http
        def spy(url, secret, payload, event_type, delivery_id):
            captured["url"] = url
            captured["secret"] = secret
            captured["payload"] = payload
            captured["event_type"] = event_type
            return {"success": True, "http_status": 200, "error_category": None,
                    "responded_at": "now", "elapsed_ms": 7}
        with patch("reseller_api.controllers.v1.webhooks._send_http",
                   side_effect=spy):
            r = self.client.post(f"/api/v1/webhooks/{whk}/test",
                                 headers={"Authorization": f"Bearer {key}.{sec}"})
        self.assertEqual(r.status_code, 200, r.get_json())
        t = r.get_json()["data"]["test"]
        self.assertTrue(t["delivered"])
        self.assertEqual(t["http_status"], 200)
        self.assertEqual(t["event_type"], "test.ping")
        # signed with the webhook's own (test) secret, to its own URL
        self.assertEqual(captured["url"], "https://sb.example/hook")
        self.assertEqual(captured["secret"], hook_secret)
        self.assertEqual(captured["payload"]["event"], "test.ping")
        self.assertEqual(captured["payload"]["environment"], "test")
        self.assertTrue(captured["payload"]["idempotency_key"])
        # no side effects: no order, no outbox row
        db.cur.execute("SELECT COUNT(*) FROM orders WHERE reseller_id=?", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 0)
        db.cur.execute(
            "SELECT COUNT(*) FROM webhook_outbox WHERE reseller_id=? AND event_type='test.ping'",
            (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 0)
        # audited
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_security_events"
            " WHERE reseller_id=? AND event_type='webhook_test_ping'", (rs["id"],))
        self.assertGreaterEqual(int(db.cur.fetchone()[0]), 1)

    def test_webhook_ping_isolation_and_failures(self):
        rs1 = _make_reseller()
        rs2 = _make_reseller()
        key1, sec1 = rdb.create_api_key(rs1["id"], "test")
        key2, sec2 = rdb.create_api_key(rs2["id"], "test")
        hook, _ = rdb.create_webhook(rs1["id"], "https://sb1.example/hook")
        whk = f"WHK-{hook['id']:08d}"

        # foreign webhook -> uniform 404
        r = self.client.post(f"/api/v1/webhooks/{whk}/test",
                             headers={"Authorization": f"Bearer {key2}.{sec2}"})
        self.assertEqual(r.status_code, 404)

        # unknown id -> same 404
        r = self.client.post("/api/v1/webhooks/WHK-99999999/test",
                             headers={"Authorization": f"Bearer {key2}.{sec2}"})
        self.assertEqual(r.status_code, 404)

        # disabled webhook -> 422
        rdb.set_webhook_enabled_for_reseller(rs1["id"], hook["id"], False)
        r = self.client.post(f"/api/v1/webhooks/{whk}/test",
                             headers={"Authorization": f"Bearer {key1}.{sec1}"})
        self.assertEqual(r.status_code, 422)

        # unreachable target -> delivered=false, truthful error category
        rdb.set_webhook_enabled_for_reseller(rs1["id"], hook["id"], True)
        with patch("reseller_api.controllers.v1.webhooks._send_http",
                   return_value={"success": False, "http_status": None,
                                 "error_category": "connection", "responded_at": "now",
                                 "elapsed_ms": 3}):
            r = self.client.post(f"/api/v1/webhooks/{whk}/test",
                                 headers={"Authorization": f"Bearer {key1}.{sec1}"})
        self.assertEqual(r.status_code, 200)
        t = r.get_json()["data"]["test"]
        self.assertFalse(t["delivered"])
        self.assertEqual(t["error_category"], "connection")

        # ping requires auth
        r = self.client.post(f"/api/v1/webhooks/{whk}/test")
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
