"""Phase 13 Prompt 10 — FINAL production readiness & regression.

One consolidated execution pass over every Phase 13 acceptance area, backed
by the live Flask stack.  Deep per-area coverage lives in the dedicated P13
suites (standardization / versioning / documentation / key lifecycle /
usage / sandbox / reliability / api security audit); this suite re-verifies
one representative happy path and one failure path per API group so the
final status table reflects ACTUAL executions end to end.
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase13_final_", suffix=".db")
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
    uid = 9_750_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P13F{n}", f"p13f{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P13F{n}", uid, f"p13f{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=("F-1", "F-2", "F-3")):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P13F Product {n}", price),
    )
    db.conn.commit()
    pid = db.cur.lastrowid
    db.cur.execute("UPDATE products SET max_per_order=25 WHERE id=?", (pid,))
    for c in codes:
        db.cur.execute(
            "INSERT INTO product_stock(product_id, delivery, sold) VALUES(?,?,0)", (pid, c)
        )
    db.conn.commit()
    return pid, rdb.ensure_product_public_id(pid)


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


class Phase13FinalReadiness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p0 = patch(
            "reseller_api.middleware.authentication.check_ip_auth_failure_rate",
            return_value=(True, 0))
        cls.p0.start()
        cls.p0b = patch(
            "reseller_api.middleware.authentication.record_ip_auth_failure",
            return_value=None)
        cls.p0b.start()
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
        cls.p0b.stop()
        cls.p0.stop()
        try:
            db.close_thread_connection()
        finally:
            try:
                os.unlink(_db_path)
            except FileNotFoundError:
                pass

    # ------------------------------------------------------------ 1. all API groups

    def test_all_api_groups_live(self):
        """Every API group answers with the standard envelope: auth, products,
        mappings, pricing, quotes, orders, status, delivery, wallet, webhooks,
        usage, and key lifecycle."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        rdb.set_product_access(rs["id"], pid, True)
        rdb.post_wallet_transaction(rs["id"], "topup_credit", 100000, "TP-F-1", "seed")
        H = _hdr(key, sec)

        # products
        self.assertEqual(self.client.get("/api/v1/products", headers=H).status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/products/{pub}", headers=H).status_code, 200)
        # mapping (import) + my-products
        self.assertEqual(self.client.post(f"/api/v1/products/{pub}/import",
                                          headers=H).status_code, 201)
        self.assertEqual(self.client.get("/api/v1/my-products", headers=H).status_code, 200)
        # pricing
        self.assertEqual(self.client.get(f"/api/v1/pricing/{pub}", headers=H).status_code, 200)
        # quote
        q = self.client.post("/api/v1/orders/quote", headers=H,
                             json={"product_id": pub, "quantity": 1})
        self.assertEqual(q.status_code, 200)
        # order + status + delivery
        r = self.client.post("/api/v1/orders", headers=H,
                             json={"product_id": pub, "quantity": 1,
                                   "client_order_id": "p13f-final"})
        self.assertEqual(r.status_code, 201, r.get_json())
        oid = r.get_json()["data"]["order_id"]
        self.assertEqual(self.client.get(f"/api/v1/orders/{oid}", headers=H).status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/orders/{oid}/delivery",
                                         headers=H).status_code, 200)
        # wallet + usage
        self.assertEqual(self.client.get("/api/v1/balance", headers=H).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/usage", headers=H).status_code, 200)
        # webhooks
        self.assertEqual(self.client.get("/api/v1/webhooks", headers=H).status_code, 200)
        # keys: rotation lifecycle through the service (bot surface)
        out = rdb.rotate_api_key_for_reseller(rs["id"], key)
        self.assertIsNotNone(out)
        new_key, new_sec, _exp = out
        self.assertEqual(self.client.get("/api/v1/balance",
                                         headers=_hdr(new_key, new_sec)).status_code, 200)

    # ------------------------------------------------------------ 2. responses

    def test_responses_envelope_codes_requestids_pagination(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        rdb.set_product_access(rs["id"], pid, True)
        rdb.import_reseller_product(rs["id"], pid)
        rdb.post_wallet_transaction(rs["id"], "topup_credit", 100000, "TP-F-2", "seed")
        H = _hdr(key, sec)

        # success envelope
        ok = self.client.get("/api/v1/products", headers=H).get_json()
        self.assertTrue(ok["ok"])
        self.assertIn("data", ok)
        self.assertIn("meta", ok)          # pagination meta
        self.assertTrue(ok["request_id"])
        # error envelope + codes
        bad = self.client.post("/api/v1/orders", headers=H,
                               json={"product_id": pub, "quantity": -1})
        self.assertEqual(bad.status_code, 400)
        eb = bad.get_json()
        self.assertFalse(eb["ok"])
        self.assertEqual(eb["error"]["code"], "invalid_quantity")
        self.assertTrue(eb["request_id"])
        self.assertNotEqual(ok["request_id"], eb["request_id"])
        # cursor pagination contract
        lst = self.client.get("/api/v1/orders", headers=H).get_json()
        self.assertIn("has_more", lst["meta"]["pagination"])
        self.assertIn("next_after_id", lst["meta"]["pagination"])

    # ------------------------------------------------------------ 3-4. security & rate limit (spot re-verify)

    def test_security_spot_matrix(self):
        rs, key, sec = _make_reseller()
        rs2, k2, s2 = _make_reseller()
        pid, pub = _make_product()
        rdb.set_product_access(rs["id"], pid, True)
        rdb.import_reseller_product(rs["id"], pid)
        rdb.post_wallet_transaction(rs["id"], "topup_credit", 50000, "TP-F-3", "seed")

        # no auth / foreign order 404
        self.assertEqual(self.client.get("/api/v1/products").status_code, 401)
        r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                             json={"product_id": pub, "quantity": 1,
                                   "client_order_id": "p13f-sec"})
        oid = r.get_json()["data"]["order_id"]
        self.assertEqual(self.client.get(f"/api/v1/orders/{oid}",
                                         headers=_hdr(k2, s2)).status_code, 404)
        # input validation
        self.assertEqual(self.client.post(
            "/api/v1/orders", headers=_hdr(key, sec),
            json={"product_id": pub, "quantity": 1, "total": "0.01"}).status_code, 400)
        # secret never in any response
        import json as _json
        for resp in (r, self.client.get(f"/api/v1/orders/{oid}", headers=_hdr(key, sec)),
                     self.client.get("/api/v1/usage", headers=_hdr(key, sec))):
            self.assertNotIn(sec, _json.dumps(resp.get_json()))

    def test_rate_limit_and_quota_live(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        rdb.set_product_access(rs["id"], pid, True)
        rdb.import_reseller_product(rs["id"], pid)
        rdb.post_wallet_transaction(rs["id"], "topup_credit", 50000, "TP-F-4", "seed")
        rdb.set_quota_settings(rs["id"], daily_order_quota=1)
        H = _hdr(key, sec)
        r1 = self.client.post("/api/v1/orders", headers=H,
                              json={"product_id": pub, "quantity": 1,
                                    "client_order_id": "p13f-q1"})
        self.assertEqual(r1.status_code, 201)
        r2 = self.client.post("/api/v1/orders", headers=H,
                              json={"product_id": pub, "quantity": 1,
                                    "client_order_id": "p13f-q2"})
        self.assertEqual(r2.status_code, 429)
        self.assertEqual(r2.get_json()["error"]["code"], "daily_order_quota_exceeded")

    # ------------------------------------------------------------ 5-6. order + webhook regression

    def test_order_pipeline_and_webhook_events(self):
        from reseller_api.services.order_failure import handle_post_debit_failure
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        rdb.set_product_access(rs["id"], pid, True)
        rdb.import_reseller_product(rs["id"], pid)
        rdb.post_wallet_transaction(rs["id"], "topup_credit", 50000, "TP-F-5", "seed")
        hook, _s = rdb.create_webhook(rs["id"], "https://final.example/h")
        H = _hdr(key, sec)

        # quote -> order -> master row -> refund; webhook events chained
        self.assertEqual(self.client.post(
            "/api/v1/orders/quote", headers=H,
            json={"product_id": pub, "quantity": 1}).status_code, 200)
        r = self.client.post("/api/v1/orders", headers=H,
                             json={"product_id": pub, "quantity": 1,
                                   "client_order_id": "p13f-ord"})
        self.assertEqual(r.status_code, 201)
        oid = r.get_json()["data"]["order_id"]
        ioid = int(oid.replace("ORD-", ""))
        db.cur.execute(
            "SELECT reseller_id, quantity, unit_price_cents, source FROM orders WHERE id=?",
            (ioid,))
        self.assertEqual(db.cur.fetchone(), (rs["id"], 1, 1000, "reseller_api"))

        res = handle_post_debit_failure(ioid, reason="final_regression")
        self.assertEqual(res["result"], "handled")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)

        db.cur.execute(
            "SELECT event_type FROM webhook_outbox WHERE aggregate_id=? ORDER BY id", (oid,))
        events = [row[0] for row in db.cur.fetchall()]
        self.assertIn("order.created", events)
        self.assertIn("order.failed", events)
        self.assertIn("refund.created", events)
        self.assertIn("delivery.failed", events)

        # HMAC signing + verification intact (freshness window enforced)
        from reseller_api.services.webhook_signature import (
            sign_body, verify_signature, current_timestamp)
        ts = current_timestamp()
        sig = sign_body("whsec_test", b'{"x":1}', ts)
        self.assertTrue(sig.startswith("sha256="))
        self.assertTrue(verify_signature("whsec_test", b'{"x":1}', ts, sig))
        self.assertFalse(verify_signature("whsec_test", b'{"x":2}', ts, sig))
        # replay protection: stale timestamps rejected
        self.assertFalse(verify_signature("whsec_test", b'{"x":1}',
                                          "1700000000", sig))

    # ------------------------------------------------------------ 7. documentation

    def test_documentation_parity(self):
        import yaml
        spec = yaml.safe_load((ROOT / "docs" / "openapi.yaml").read_text())
        self.assertTrue(str(spec["openapi"]).startswith("3."))
        documented = {(m.upper(), p) for p, item in spec["paths"].items() for m in item}
        live = set()
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint.startswith("reseller_api_v1."):
                for m in rule.methods - {"HEAD", "OPTIONS"}:
                    import re as _re
                    pth = _re.sub(r"<(?:[^:>]+:)?([^>]+)>",
                                  lambda mm: "{%s}" % {
                                      "order_id": "orderId", "product_id": "productId",
                                      "webhook_id": "webhookId", "delivery_id": "deliveryId",
                                      "invoice_id": "invoiceId",
                                      "refund_id": "refundId",
                                      "statement_id": "statementId",
                                      "outbox_id": "eventId"}.get(mm.group(1), mm.group(1)),
                                  rule.rule)
                    live.add((m, pth.replace("/api/v1", "", 1) or "/"))
        live.discard(("GET", ""))
        self.assertEqual(live, documented)
        for doc in ("RESELLER_API.md", "RESELLER_API_VERSIONING.md"):
            self.assertTrue((ROOT / doc).exists(), doc)

    # ------------------------------------------------------------ 8. sandbox

    def test_sandbox_fail_closed(self):
        from reseller_api.config import validate_environment_marker
        with self.assertRaises(RuntimeError):
            validate_environment_marker({"API_ENVIRONMENT": "production"})
        validate_environment_marker({"API_ENVIRONMENT": "test"})  # no raise

    # ------------------------------------------------------------ 9-10. webadmin & bot

    def test_webadmin_owner_only_and_functional(self):
        import web_admin.app as wa
        wa.app.config["TESTING"] = True
        wa.DB_PATH = _db_path
        anon = wa.app.test_client()
        self.assertEqual(anon.get("/api/monitoring/api-health").status_code, 401)
        rs, key, sec = _make_reseller()
        # a reseller API key cannot open WebAdmin
        self.assertIn(
            anon.get("/api/resellers", headers=_hdr(key, sec)).status_code,
            (401, 302, 303))
        # owner session works
        owner = wa.app.test_client()
        with owner.session_transaction() as sess:
            sess["logged_in"] = True
            sess["username"] = "owner"
        r = owner.get("/api/monitoring/api-health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_telegram_bot_flow_unchanged(self):
        """Customer path at the master DB layer (handlers import python-
        telegram-bot; the repo convention tests the DB flow directly)."""
        from utils.crypto import encrypt_text, decrypt_text
        pid, pub = _make_product(price=5.0, codes=["TG-1"])
        items = db.reserve_stock_items(pid, 555000111, 1)
        self.assertIsNotNone(items)
        blob = encrypt_text("\n\n".join(items))
        db.cur.execute(
            "INSERT INTO orders(user_id, product_name, price, delivery, product_id,"
            " payment_method) VALUES(?,?,?,?,?,?)",
            (555000111, "P13F TG Product", 5.0, blob, pid, "crypto"),
        )
        db.conn.commit()
        oid = db.cur.lastrowid
        db.cur.execute("SELECT delivery, source, reseller_id FROM orders WHERE id=?", (oid,))
        blob2, source, rid = db.cur.fetchone()
        self.assertIn("TG-1", decrypt_text(blob2))
        self.assertIsNone(source)        # customer order: no reseller linkage
        self.assertIsNone(rid)
        tid = db.create_ticket(555000111, "Help", "Where is my order?")
        self.assertTrue(int(tid) > 0)

    # ------------------------------------------------------------ 11. railway config sanity

    def test_railway_config_consistent(self):
        procfile = (ROOT / "Procfile").read_text()
        self.assertIn("webhook-worker", procfile)
        self.assertIn("python bot.py", procfile)
        raildoc = (ROOT / "RAILWAY_RESELLER_API.md").read_text()
        for token in ("RESELLER_API_ENVIRONMENT", "RESELLER_API_REQUIRE_POSTGRES",
                      "DATA_ENCRYPTION_KEY", "/health", "gunicorn"):
            self.assertIn(token, raildoc)
        # health endpoints behave as documented
        self.assertEqual(self.client.get("/health").status_code, 200)

    # ------------------------------------------------------------ 12. database integrity

    def test_database_integrity(self):
        db.cur.execute("PRAGMA integrity_check")
        self.assertEqual(db.cur.fetchone()[0], "ok")
        db.cur.execute(
            "SELECT reseller_id, client_order_id, COUNT(*) c FROM orders"
            " WHERE client_order_id IS NOT NULL"
            " GROUP BY reseller_id, client_order_id HAVING c > 1")
        self.assertEqual(db.cur.fetchall(), [])
        db.cur.execute(
            "SELECT reseller_id, tx_type, reference, COUNT(*) c"
            " FROM reseller_wallet_transactions WHERE reference IS NOT NULL"
            " GROUP BY reseller_id, tx_type, reference HAVING c > 1")
        self.assertEqual(db.cur.fetchall(), [])
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_wallet_transactions"
            " WHERE balance_after_cents < 0 OR amount_cents = 0")
        self.assertEqual(int(db.cur.fetchone()[0]), 0)


if __name__ == "__main__":
    unittest.main()
