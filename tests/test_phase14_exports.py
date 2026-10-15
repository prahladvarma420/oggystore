"""Phase 14 Prompt 8 — analytics reports & safe CSV export.

Reseller self-export (/api/v1/exports/orders.csv) and owner platform exports
(/api/export/<kind>) verified against known data: correctness, isolation,
filters, caps, streaming shape, audit logging, and secret hygiene.
"""

import csv
import io
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase14_exp_", suffix=".db")
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
    uid = 9_680_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P14E{n}", f"p14e{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P14E{n}", uid, f"p14e{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=()):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P14E Product {n}", price),
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


def _grant(rid, pid):
    rdb.set_product_access(rid, pid, True)
    rdb.import_reseller_product(rid, pid)


def _topup(rid, cents):
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P14E-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


def _read_csv(resp):
    text = resp.get_data(as_text=True)
    rows = list(csv.reader(io.StringIO(text)))
    return rows[0], rows[1:]


class Phase14ExportsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # persist_audit_record is NOT patched: the api_usage export reads
        # the real per-request log rows this suite generates.
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

        # Reseller A: 3 orders (2 ok, 1 failed+refunded)
        cls.ra, cls.ka, cls.sa = _make_reseller()
        cls.pa, cls.puba = _make_product(10.0, codes=["EA-1", "EA-2", "EA-3", "EA-4"])
        _grant(cls.ra["id"], cls.pa)
        _topup(cls.ra["id"], 100000)
        H = _hdr(cls.ka, cls.sa)
        for i in (1, 2):
            r = cls.client.post("/api/v1/orders", headers=H,
                                json={"product_id": cls.puba, "quantity": 1,
                                      "client_order_id": f"p14e-a{i}"})
            assert r.status_code == 201
        r3 = cls.client.post("/api/v1/orders", headers=H,
                             json={"product_id": cls.puba, "quantity": 1,
                                   "client_order_id": "p14e-a3"})
        assert r3.status_code == 201
        cls.a3 = int(r3.get_json()["data"]["order_id"].replace("ORD-", ""))
        handle_post_debit_failure(cls.a3, reason="export_test")

        # Reseller B: 1 order
        cls.rb, cls.kb, cls.sb = _make_reseller()
        cls.pb, cls.pubb = _make_product(20.0, codes=["EB-1"])
        _grant(cls.rb["id"], cls.pb)
        _topup(cls.rb["id"], 100000)
        rb1 = cls.client.post("/api/v1/orders", headers=_hdr(cls.kb, cls.sb),
                              json={"product_id": cls.pubb, "quantity": 1,
                                    "client_order_id": "p14e-b1"})
        assert rb1.status_code == 201
        cls.b1_oid = rb1.get_json()["data"]["order_id"]

        # one webhook delivery (mocked HTTP 200) so webhook_activity has rows
        cls.hook_b, _ = rdb.create_webhook(cls.rb["id"], "https://exp.example/h")
        from reseller_api.workers.webhook_worker import _process_event
        from reseller_api.services.webhook_outbox import get_outbox_row
        from reseller_api.services import webhook_delivery as wd
        ok200 = {"success": True, "http_status": 200, "error_category": None,
                 "responded_at": "t", "elapsed_ms": 5}
        db.cur.execute(
            "SELECT id FROM webhook_outbox WHERE reseller_id=?", (cls.rb["id"],))
        with patch.object(wd, "_send_http", return_value=ok200):
            for (eid,) in db.cur.fetchall():
                _process_event(get_outbox_row(eid))

    @classmethod
    def tearDownClass(cls):
        cls.p3.stop()
        cls.p2.stop()
        try:
            db.close_thread_connection()
        finally:
            try:
                os.unlink(_db_path)
            except FileNotFoundError:
                pass

    def _owner(self, role="owner"):
        import web_admin.app as wa
        wa.app.config["TESTING"] = True
        wa.DB_PATH = _db_path
        c = wa.app.test_client()
        with c.session_transaction() as sess:
            sess["logged_in"] = True
            sess["username"] = "owner"
            sess["web_role"] = role
        return c

    # ---------------------------------------------------------- reseller export

    def test_reseller_orders_csv_correct_and_isolated(self):
        r = self.client.get("/api/v1/exports/orders.csv", headers=_hdr(self.ka, self.sa))
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r.content_type)
        self.assertIn("attachment", r.headers.get("Content-Disposition", ""))
        header, rows = _read_csv(r)
        self.assertEqual(header[0], "order_id")
        self.assertIn("refund_total", header)
        self.assertEqual(len(rows), 3)                      # only A's orders
        oids = {row[0] for row in rows}
        self.assertNotIn(self.b1_oid, oids)                 # B never leaks
        self.assertIn(f"ORD-{self.a3:08d}", oids)
        # refund columns correct
        by_id = {row[0]: dict(zip(header, row)) for row in rows}
        failed_row = by_id[f"ORD-{self.a3:08d}"]
        self.assertEqual(failed_row["status"], "failed")
        self.assertEqual(failed_row["refunded"], "true")
        self.assertEqual(failed_row["refund_total"], "10.00")
        ok_row = by_id[[o for o in oids if o != f"ORD-{self.a3:08d}"][0]]
        self.assertEqual(ok_row["refunded"], "false")
        self.assertEqual(ok_row["total"], "10.00")
        # B sees only his own
        rb = self.client.get("/api/v1/exports/orders.csv", headers=_hdr(self.kb, self.sb))
        _h, rows_b = _read_csv(rb)
        self.assertEqual(len(rows_b), 1)
        self.assertEqual(rows_b[0][0], self.b1_oid)

    def test_reseller_export_no_sensitive_content(self):
        r = self.client.get("/api/v1/exports/orders.csv", headers=_hdr(self.ka, self.sa))
        text = r.get_data(as_text=True)
        for banned in ("EA-1", "EA-2", "EA-3", "secret", "reseller_id",
                       "user_id", "unit_price_cents"):
            self.assertNotIn(banned, text)   # no delivery codes, no internals

    def test_reseller_export_filters_and_validation(self):
        from utils.timezone import now as _now
        today = _now().strftime("%Y-%m-%d")
        r = self.client.get(f"/api/v1/exports/orders.csv?date_from={today}&date_to={today}",
                            headers=_hdr(self.ka, self.sa))
        self.assertEqual(len(_read_csv(r)[1]), 3)
        r = self.client.get("/api/v1/exports/orders.csv?date_from=2999-01-01",
                            headers=_hdr(self.ka, self.sa))
        header, rows = _read_csv(r)
        self.assertEqual(rows, [])                          # empty report = headers only
        self.assertTrue(header)
        r = self.client.get("/api/v1/exports/orders.csv?date_from=nope",
                            headers=_hdr(self.ka, self.sa))
        self.assertEqual(r.status_code, 400)
        # unauthenticated
        self.assertEqual(self.client.get("/api/v1/exports/orders.csv").status_code, 401)

    def test_reseller_export_audited(self):
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_security_events"
            " WHERE reseller_id=? AND event_type='data_export'", (self.ra["id"],))
        self.assertGreaterEqual(int(db.cur.fetchone()[0]), 1)

    def test_concurrent_exports_consistent(self):
        results = []
        barrier = threading.Barrier(4)

        def worker():
            c = self.app.test_client()
            barrier.wait()
            r = c.get("/api/v1/exports/orders.csv", headers=_hdr(self.ka, self.sa))
            results.append(r.get_data(as_text=True))

        ts = [threading.Thread(target=worker) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(set(results)), 1)              # identical content
        self.assertEqual(len(list(csv.reader(io.StringIO(results[0])))) - 1, 3)

    # ---------------------------------------------------------- owner exports

    def test_owner_export_kinds(self):
        ac = self._owner()
        cases = {
            "reseller_orders": ("reseller", "product", "quantity"),
            "wallet_transactions": ("tx_type", "amount_cents", "reference"),
            "api_usage": ("endpoint", "status_code", "request_id"),
            "webhook_activity": ("webhook", "event_type", "success"),
            "product_sales": ("product_id", "units_sold", "gross_sales"),
        }
        for kind, expect_cols in cases.items():
            r = ac.get(f"/api/export/{kind}")
            self.assertEqual(r.status_code, 200, kind)
            self.assertIn("text/csv", r.content_type, kind)
            header, rows = _read_csv(r)
            for col in expect_cols:
                self.assertIn(col, header, f"{kind} missing {col}")
            self.assertGreaterEqual(len(rows), 1, f"{kind} empty")
        # orders export (existing) now bounded + dated
        r = ac.get("/api/export/orders")
        self.assertEqual(r.status_code, 200)
        header, _rows = _read_csv(r)
        self.assertNotIn("delivery", header)                # no delivery content
        self.assertIn("source", header)

    def test_owner_export_filters_and_gates(self):
        ac = self._owner()
        from utils.timezone import now as _now
        today = _now().strftime("%Y-%m-%d")
        r = ac.get(f"/api/export/wallet_transactions?date_from={today}&date_to={today}")
        _read_csv(r)   # consume the stream (test-client contexts require it)
        self.assertEqual(r.status_code, 200)
        r = ac.get("/api/export/wallet_transactions?date_from=2999-01-01")
        _h, rows = _read_csv(r)
        self.assertEqual(rows, [])
        r = ac.get("/api/export/api_usage?date_from=garbage")
        self.assertEqual(r.status_code, 400)
        # login gate
        import web_admin.app as wa
        anon = wa.app.test_client()
        self.assertEqual(anon.get("/api/export/reseller_orders").status_code, 401)
        # logged-in non-owner role rejected (owner_required)
        non_owner = self._owner(role="admin")
        self.assertEqual(non_owner.get("/api/export/reseller_orders").status_code, 403)

    def test_owner_exports_no_secrets_or_delivery(self):
        ac = self._owner()
        for kind in ("reseller_orders", "wallet_transactions", "api_usage",
                     "webhook_activity", "product_sales"):
            text = ac.get(f"/api/export/{kind}").get_data(as_text=True).lower()
            for banned in ("secret", "secret_hash", "whsec", "ea-1", "eb-1"):
                self.assertNotIn(banned, text, f"{kind} leaked {banned}")

    def test_owner_export_audit_logged(self):
        ac = self._owner()
        db.cur.execute("SELECT COUNT(*) FROM admin_logs WHERE action LIKE '%export%'")
        before = int(db.cur.fetchone()[0])
        _read_csv(ac.get("/api/export/reseller_orders"))
        _read_csv(ac.get("/api/export/api_usage"))
        db.cur.execute("SELECT COUNT(*) FROM admin_logs WHERE action LIKE '%export%'")
        after = int(db.cur.fetchone()[0])
        self.assertGreaterEqual(after - before, 2)
        db.cur.execute(
            "SELECT details FROM admin_logs WHERE action LIKE '%export%' ORDER BY id DESC LIMIT 1")
        self.assertIn("rows=", db.cur.fetchone()[0])


if __name__ == "__main__":
    unittest.main()
