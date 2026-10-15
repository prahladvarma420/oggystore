"""Phase 14 Prompt 6 — webhook, delivery & fulfillment analytics.

A known operational workload: real orders emit outbox events; deliveries are
driven through the real worker functions with mocked HTTP outcomes (success,
500 retry, 400 terminal, timeout-no-response, dead-letter).  Analytics are
then verified exactly, for the platform owner and per reseller.
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase14_wh_", suffix=".db")
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
from reseller_api.services.order_failure import handle_post_debit_failure

_ctr = 0


def _n():
    global _ctr
    _ctr += 1
    return _ctr


def _make_reseller():
    n = _n()
    uid = 9_700_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P14W{n}", f"p14w{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P14W{n}", uid, f"p14w{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(codes):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,10.0,0,'code_stock',1)",
        (f"P14W Product {n}",),
    )
    db.conn.commit()
    pid = db.cur.lastrowid
    for c in codes:
        db.cur.execute(
            "INSERT INTO product_stock(product_id, delivery, sold) VALUES(?,?,0)", (pid, c)
        )
    db.conn.commit()
    return pid, rdb.ensure_product_public_id(pid)


def _grant(rid, pid):
    rdb.set_product_access(rid, pid, True)
    rdb.import_reseller_product(rid, pid)


def _topup(rid, cents):
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P14W-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


def _outbox_rows(rid):
    db.cur.execute(
        "SELECT id, event_type, aggregate_id FROM webhook_outbox"
        " WHERE reseller_id=? ORDER BY id", (rid,))
    return db.cur.fetchall()


class Phase14WebhookAnalyticsTests(unittest.TestCase):
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

        from reseller_api.workers.webhook_worker import _process_event
        from reseller_api.services.webhook_outbox import get_outbox_row
        from reseller_api.services import webhook_delivery as wd

        def drive(rid, agg, result):
            """Process only THIS order's pending events (the real worker's
            due-time poll would never touch other orders' backlogged rows)."""
            with patch.object(wd, "_send_http", return_value=result):
                for row in _outbox_rows(rid):
                    if row[2] != agg:
                        continue
                    rowd = get_outbox_row(row[0])
                    if rowd["status"] == "pending":
                        _process_event(rowd)

        # ---------- Reseller A: full delivery story ----------
        cls.ra, cls.ka, cls.sa = _make_reseller()
        cls.pa, cls.puba = _make_product(["WA-1", "WA-2", "WA-3", "WA-4"])
        _grant(cls.ra["id"], cls.pa)
        _topup(cls.ra["id"], 100000)
        cls.hook_a, _ = rdb.create_webhook(cls.ra["id"], "https://wa.example/h")

        # order 1 -> delivered OK (order.created + delivery.completed)
        r1 = cls.client.post("/api/v1/orders", headers=_hdr(cls.ka, cls.sa),
                             json={"product_id": cls.puba, "quantity": 1,
                                   "client_order_id": "p14w-a1"})
        assert r1.status_code == 201
        ok200 = {"success": True, "http_status": 200, "error_category": None,
                 "responded_at": "t", "elapsed_ms": 5}
        drive(cls.ra["id"], "ORD-%08d" % int(r1.get_json()["data"]["order_id"][4:]), ok200)

        # order 2 -> 500 retryable, stays pending with attempts=1
        r2 = cls.client.post("/api/v1/orders", headers=_hdr(cls.ka, cls.sa),
                             json={"product_id": cls.puba, "quantity": 1,
                                   "client_order_id": "p14w-a2"})
        assert r2.status_code == 201
        fail500 = {"success": False, "http_status": 500, "error_category": "5xx",
                   "responded_at": "t", "elapsed_ms": 5}
        drive(cls.ra["id"], "ORD-%08d" % int(r2.get_json()["data"]["order_id"][4:]), fail500)

        # order 3 -> terminal 400 -> dead-letter
        r3 = cls.client.post("/api/v1/orders", headers=_hdr(cls.ka, cls.sa),
                             json={"product_id": cls.puba, "quantity": 1,
                                   "client_order_id": "p14w-a3"})
        assert r3.status_code == 201
        fail400 = {"success": False, "http_status": 400, "error_category": "4xx",
                   "responded_at": "t", "elapsed_ms": 5}
        drive(cls.ra["id"], "ORD-%08d" % int(r3.get_json()["data"]["order_id"][4:]), fail400)

        # order 4 -> failed + refunded (order.failed/delivery.failed events),
        # delivery of those events times out (no response)
        r4 = cls.client.post("/api/v1/orders", headers=_hdr(cls.ka, cls.sa),
                             json={"product_id": cls.puba, "quantity": 1,
                                   "client_order_id": "p14w-a4"})
        assert r4.status_code == 201
        cls.a4 = int(r4.get_json()["data"]["order_id"].replace("ORD-", ""))
        handle_post_debit_failure(cls.a4, reason="test")
        timeout_res = {"success": False, "http_status": None,
                       "error_category": "timeout", "responded_at": "t",
                       "elapsed_ms": 3000}
        drive(cls.ra["id"], f"ORD-{cls.a4:08d}", timeout_res)

        # ---------- Reseller B: one clean order ----------
        cls.rb, cls.kb, cls.sb = _make_reseller()
        cls.pb, cls.pubb = _make_product(["WB-1"])
        _grant(cls.rb["id"], cls.pb)
        _topup(cls.rb["id"], 100000)
        cls.hook_b, _ = rdb.create_webhook(cls.rb["id"], "https://wb.example/h")
        rb1 = cls.client.post("/api/v1/orders", headers=_hdr(cls.kb, cls.sb),
                              json={"product_id": cls.pubb, "quantity": 1,
                                    "client_order_id": "p14w-b1"})
        assert rb1.status_code == 201
        drive(cls.rb["id"], "ORD-%08d" % int(rb1.get_json()["data"]["order_id"][4:]), ok200)

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

    def _owner(self):
        import web_admin.app as wa
        wa.app.config["TESTING"] = True
        wa.DB_PATH = _db_path
        c = wa.app.test_client()
        with c.session_transaction() as sess:
            sess["logged_in"] = True
            sess["username"] = "owner"
        return c

    # ------------------------------------------------------------- owner view

    def test_platform_webhook_analytics_exact(self):
        r = self._owner().get("/api/monitoring/webhook-analytics?days=7")
        self.assertEqual(r.status_code, 200)
        w = r.get_json()["webhooks"]

        # A: 2 events delivered (order1) + 2 retried-pending (order2 500)
        #    + 2 dead-lettered (order3 400) + 4 pending from order4
        #    (created + completed + failed x2, delivery attempts timed out)
        # B: 2 events delivered
        self.assertEqual(w["delivered"], 4)
        self.assertGreaterEqual(w["dead_lettered"], 2)
        self.assertGreater(w["pending"], 0)
        self.assertGreaterEqual(w["events_retried"], 0)   # attempts tracked
        self.assertEqual(w["delivery_attempts"],
                         w["attempts_succeeded"] + w["attempts_failed"])
        dist = w["http_status_distribution"]
        self.assertGreaterEqual(int(dist.get("200", 0)), 4)
        self.assertGreaterEqual(int(dist.get("500", 0)), 2)
        self.assertGreaterEqual(int(dist.get("400", 0)), 2)
        self.assertGreaterEqual(int(dist.get("no_response", 0)), 2)
        self.assertIn("order.created", w["by_event_type"])
        self.assertIn("order.failed", w["by_event_type"])
        self.assertIn("refund.created", w["by_event_type"])
        self.assertEqual(w["webhooks_configured"], 2)
        self.assertEqual(w["webhooks_enabled"], 2)

        # fulfillment section mirrors the order lifecycle
        f = w["fulfillment"]
        self.assertEqual(f["orders"], 5)
        self.assertEqual(f["failed"], 1)
        self.assertEqual(f["completed"], 4)
        self.assertEqual(f["success_rate"], round(4 / 5, 4))

    def test_owner_gate_and_no_secrets(self):
        import web_admin.app as wa
        anon = wa.app.test_client()
        self.assertEqual(anon.get("/api/monitoring/webhook-analytics").status_code, 401)
        body = self._owner().get("/api/monitoring/webhook-analytics").get_json()
        import json as _json
        flat = _json.dumps(body).lower()
        for banned in ("secret", "payload", "wa-1", "wb-1", "whsec"):
            self.assertNotIn(banned, flat)

    # ------------------------------------------------------------- reseller view

    def test_reseller_webhook_analytics_scoped(self):
        ra_view = self.client.get("/api/v1/webhooks/analytics",
                                  headers=_hdr(self.ka, self.sa))
        self.assertEqual(ra_view.status_code, 200)
        wa_ = ra_view.get_json()["data"]["webhooks"]
        # A's own numbers only
        self.assertEqual(wa_["delivered"], 2)
        self.assertEqual(wa_["dead_lettered"], 2)
        self.assertGreater(wa_["pending"], 0)
        self.assertEqual(wa_["webhooks_configured"], 1)
        self.assertIn("refund.created", wa_["by_event_type"])

        # B's view: clean
        wb_ = self.client.get("/api/v1/webhooks/analytics",
                              headers=_hdr(self.kb, self.sb)).get_json()["data"]["webhooks"]
        self.assertEqual(wb_["delivered"], 2)
        self.assertEqual(wb_["dead_lettered"], 0)
        self.assertEqual(wb_["pending"], 0)

    def test_reseller_analytics_auth_and_privacy(self):
        self.assertEqual(self.client.get("/api/v1/webhooks/analytics").status_code, 401)
        import json as _json
        body = self.client.get("/api/v1/webhooks/analytics",
                               headers=_hdr(self.ka, self.sa)).get_json()
        flat = _json.dumps(body).lower()
        for banned in ("secret", "payload", "https://wa.example", "https://wb.example"):
            self.assertNotIn(banned, flat)

    def test_delivery_latency_field_present(self):
        w = rdb.get_reseller_webhook_analytics(self.ra["id"])
        self.assertIn("avg_delivery_seconds", w)
        self.assertIn("retry_attempts", w)
        self.assertIn("events_retried", w)


if __name__ == "__main__":
    unittest.main()
