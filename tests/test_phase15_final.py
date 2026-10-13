"""Phase 15.10 — final consolidated integration & regression capstone.

Runs the real Flask stack (temp SQLite, no business-logic mocks) through
every Phase 15 acceptance area: invoice lifecycle, statement lifecycle,
ledger API, adjustment workflow, admin refund flow, reconciliation
workflow, WebAdmin financial contracts, the mandatory cross-feature
ledger-equality test, prior-phase regression flows, and documentation
synchronization.
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase15_final_", suffix=".db")
os.close(_fd)

import config
config.DB_PATH = _db_path
import database.db as db
db.DB_PATH = _db_path
db._tls = threading.local()
db.init_db()

import database.reseller_db as rdb
import database.invoice_db as idb
import database.statement_db as sdb
import database.adjustment_db as adb
from reseller_api import create_app
import reseller_api.middleware.request_context  # noqa: F401
import reseller_api.middleware.rate_limit  # noqa: F401
import reseller_api.middleware.security_controls  # noqa: F401
from reseller_api.services.rate_limit import RequestRateDecision
from reseller_api.services.invoice_service import (
    InvoiceError, generate_invoice, void_invoice,
)
from reseller_api.services.statement_service import (
    StatementError, generate_statement, finalize_statement,
)
from reseller_api.services.adjustment_service import (
    AdjustmentError, create_adjustment, approve_adjustment,
    reject_adjustment, complete_adjustment, issue_admin_refund,
)
from reseller_api.services.reconciliation import (
    run_reconciliation, reconcile_reseller,
)
from reseller_api.services.order_failure import handle_post_debit_failure

_ctr = 0


def _n():
    global _ctr
    _ctr += 1
    return _ctr


def _make_reseller():
    n = _n()
    uid = 9_570_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P15F{n}", f"p15f{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P15F{n}", uid, f"p15f{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=()):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P15F Product {n}", price),
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P15F-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


def _today():
    from utils.timezone import now as _now
    return _now().strftime("%Y-%m-%d")


class Phase15FinalCapstone(unittest.TestCase):
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

    def _order(self, key, sec, pub, qty, coid):
        r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                             json={"product_id": pub, "quantity": qty,
                                   "client_order_id": coid})
        assert r.status_code == 201, r.get_json()
        return int(r.get_json()["data"]["order_id"].replace("ORD-", ""))

    def _owner(self, role="owner"):
        import web_admin.app as wa
        wa.app.config["TESTING"] = True
        wa.DB_PATH = _db_path
        c = wa.app.test_client()
        with c.session_transaction() as sess:
            sess["logged_in"] = True
            sess["username"] = "owner"
            sess["web_role"] = role
            sess["csrf"] = "test-csrf"
        return c

    # ============================================================
    # 1. INVOICE LIFECYCLE
    # ============================================================

    def test_01_invoice_lifecycle(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["F1-1", "F1-2", "F1-3"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 2, "p15f-inv1")
        self._order(key, sec, pub, 1, "p15f-inv2")
        today = _today()

        inv = generate_invoice(rs["id"], today, today, issued_by="owner")
        self.assertEqual(inv["status"], "finalized")
        self.assertEqual(inv["subtotal_cents"], 3000)
        self.assertEqual(inv["total_cents"], 3000)
        self.assertEqual(inv["amount_paid_cents"], 3000)
        self.assertEqual(inv["amount_due_cents"], 0)
        self.assertEqual(inv["orders_count"], 2)
        lines = idb.get_invoice_lines(inv["id"])
        self.assertEqual(len([l for l in lines if l["source_type"] == "purchase"]), 2)

        H = _hdr(key, sec)
        self.assertEqual(self.client.get("/api/v1/invoices", headers=H).status_code, 200)
        d = self.client.get(f"/api/v1/invoices/{inv['public_id']}", headers=H)
        self.assertEqual(d.get_json()["data"]["invoice"]["total"], "30.00")
        ls = self.client.get(f"/api/v1/invoices/{inv['public_id']}/lines",
                             headers=H)
        self.assertEqual(len(ls.get_json()["data"]["lines"]), 3)  # 2 purchases + topup
        tx = self.client.get(f"/api/v1/invoices/{inv['public_id']}/transactions",
                             headers=H)
        self.assertEqual(len(tx.get_json()["data"]["transactions"]), 3)
        doc = self.client.get(f"/api/v1/invoices/{inv['public_id']}/document",
                              headers=H)
        self.assertEqual(doc.status_code, 200)
        self.assertIn(inv["invoice_number"], doc.get_data(as_text=True))

        v = void_invoice(inv["id"], "capstone void")
        self.assertEqual(v["status"], "void")
        with self.assertRaises(InvoiceError):
            generate_invoice(99999999, today, today)
        with self.assertRaises(InvoiceError):
            generate_invoice(rs["id"], today, "2020-01-01")
        with self.assertRaises(InvoiceError):
            generate_invoice(rs["id"], "not-a-date", today)

    # ============================================================
    # 2. STATEMENT LIFECYCLE
    # ============================================================

    def test_02_statement_lifecycle(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["F2-1", "F2-2"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15f-st1")
        o2 = self._order(key, sec, pub, 1, "p15f-st2")
        handle_post_debit_failure(o2, reason="capstone")
        today = _today()

        st = generate_statement(rs["id"], period_type="custom",
                                start_date=today, end_date=today)
        self.assertEqual(st["status"], "open")
        self.assertEqual(st["opening_balance_cents"], 0)
        self.assertEqual(st["total_credits_cents"], 51000)
        self.assertEqual(st["total_debits_cents"], 2000)
        self.assertEqual(st["closing_balance_cents"], 49000)
        self.assertEqual(st["transactions_count"], 4)
        self.assertEqual(st["orders_count"], 2)
        self.assertEqual(st["refunds_cents"], 1000)

        fin = finalize_statement(st["id"])
        self.assertEqual(fin["status"], "finalized")
        fin2 = finalize_statement(st["id"])
        self.assertTrue(fin2["is_duplicate"])

        H = _hdr(key, sec)
        d = self.client.get(f"/api/v1/statements/{st['public_id']}", headers=H)
        self.assertEqual(d.get_json()["data"]["statement"]["closing_balance"],
                         "490.00")
        doc = self.client.get(f"/api/v1/statements/{st['public_id']}/document",
                              headers=H)
        self.assertEqual(doc.status_code, 200)
        self.assertIn(st["statement_number"], doc.get_data(as_text=True))
        self.assertIn("490.00", doc.get_data(as_text=True))

        with self.assertRaises(StatementError):
            generate_statement(rs["id"], period_type="bogus")
        with self.assertRaises(StatementError):
            finalize_statement(99999999)
        with self.assertRaises(StatementError):
            generate_statement(rs["id"], period_type="custom",
                               start_date=today, end_date="2020-01-01")

    # ============================================================
    # 3. TRANSACTION LEDGER API
    # ============================================================

    def test_03_ledger_api(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["F3-1", "F3-2"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        o1 = self._order(key, sec, pub, 1, "p15l-cap1")
        o1_pub = f"ORD-{o1:08d}"
        handle_post_debit_failure(o1, reason="capstone")
        self._order(key, sec, pub, 1, "p15l-cap2")
        H = _hdr(key, sec)

        r = self.client.get("/api/v1/transactions", headers=H)
        self.assertEqual(r.status_code, 200)
        rows = r.get_json()["data"]["transactions"]
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertIn("reference", row)
            self.assertIn("balance_after", row)
            self.assertIn("balance_before", row)
            self.assertNotIn("id", row)
        self.assertEqual(rows[0]["type"], "purchase_debit")

        debits = self.client.get("/api/v1/transactions?type=purchase_debit",
                                 headers=H).get_json()["data"]["transactions"]
        self.assertEqual(len(debits), 2)
        credits = self.client.get("/api/v1/transactions?direction=credit",
                                  headers=H).get_json()["data"]["transactions"]
        self.assertEqual(len(credits), 2)
        linked = self.client.get(f"/api/v1/transactions?order_id={o1_pub}",
                                 headers=H).get_json()["data"]["transactions"]
        self.assertEqual({t["type"] for t in linked},
                         {"purchase_debit", "refund_credit"})
        self.assertTrue(all(t["order_id"] == o1_pub for t in linked))
        today = _today()
        self.assertEqual(len(self.client.get(
            f"/api/v1/transactions?date_from={today}&date_to={today}",
            headers=H).get_json()["data"]["transactions"]), 4)
        self.assertEqual(self.client.get(
            "/api/v1/transactions?date_from=2999-01-01",
            headers=H).get_json()["data"]["transactions"], [])

        p1 = self.client.get("/api/v1/transactions?limit=2", headers=H).get_json()
        self.assertTrue(p1["meta"]["pagination"]["has_more"])
        after = p1["meta"]["pagination"]["next_after"]
        p2 = self.client.get(f"/api/v1/transactions?limit=2&after={after}",
                             headers=H).get_json()
        self.assertFalse({t["reference"] for t in p1["data"]["transactions"]}
                         & {t["reference"] for t in p2["data"]["transactions"]})

        self.assertEqual(self.client.get("/api/v1/transactions?type=bad",
                                         headers=H).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/transactions?limit=0",
                                         headers=H).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/transactions?after=xyz",
                                         headers=H).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/transactions").status_code, 401)

    # ============================================================
    # 4. ADJUSTMENT WORKFLOW
    # ============================================================

    def test_04_adjustment_workflow(self):
        rs, _k, _s = _make_reseller()
        _topup(rs["id"], 10000)
        adj = create_adjustment(rs["id"], "credit", 700, "capstone credit",
                                created_by="owner")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 10000)
        approve_adjustment(adj["id"], decided_by="owner")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 10000)
        complete_adjustment(adj["id"], decided_by="owner")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 10700)
        again = complete_adjustment(adj["id"], decided_by="owner")
        self.assertTrue(again["is_duplicate"])
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 10700)
        adj2 = create_adjustment(rs["id"], "debit", 300, "reject me")
        reject_adjustment(adj2["id"], decided_by="owner")
        with self.assertRaises(AdjustmentError):
            complete_adjustment(adj2["id"])
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 10700)
        db.cur.execute(
            "SELECT amount_cents, balance_after_cents FROM"
            " reseller_wallet_transactions WHERE reseller_id=? ORDER BY id",
            (rs["id"],))
        running = 0
        for amount, after in db.cur.fetchall():
            running += int(amount)
            self.assertEqual(int(after), running)

    # ============================================================
    # 5. ADMIN REFUND WORKFLOW (WebAdmin path)
    # ============================================================

    def test_05_admin_refund_workflow(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["F5-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        oid = self._order(key, sec, pub, 1, "p15f-rf1")
        ac = self._owner()
        hdr = {"X-CSRF-Token": "test-csrf"}

        r = ac.post(f"/api/orders/{oid}/admin-refund",
                    json={"amount": "4.00", "reason": "capstone partial"},
                    headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(r.get_json()["amount"], "4.00")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 49400)
        db.cur.execute(
            "SELECT amount_cents FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND tx_type='refund_credit'", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 400)
        rec = adb.list_refunds(reseller_id=rs["id"])[0]
        self.assertEqual(rec["kind"], "administrative")
        self.assertEqual(rec["ledger_reference"],
                         r.get_json()["ledger_reference"])
        db.cur.execute(
            "SELECT COUNT(*) FROM webhook_outbox WHERE reseller_id=?"
            " AND event_type='refund.created'", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 1)
        r2 = ac.post(f"/api/orders/{oid}/admin-refund",
                     json={"amount": "7.00", "reason": "over the remainder"},
                     headers=hdr)
        self.assertEqual(r2.status_code, 422)
        self.assertEqual(r2.get_json()["code"], "refund_exceeds_refundable")
        r3 = ac.post(f"/api/orders/{oid}/admin-refund",
                     json={"amount": "6.00", "reason": "rest of it"},
                     headers=hdr)
        self.assertEqual(r3.status_code, 200)
        r4 = ac.post(f"/api/orders/{oid}/admin-refund",
                     json={"amount": "0.01", "reason": "nothing left"},
                     headers=hdr)
        self.assertEqual(r4.status_code, 409)
        self.assertEqual(self._owner(role="admin").post(
            f"/api/orders/{oid}/admin-refund",
            json={"amount": "1", "reason": "x"}, headers=hdr).status_code, 403)
        import web_admin.app as wa
        anon = wa.app.test_client()
        self.assertIn(anon.post(f"/api/orders/{oid}/admin-refund",
                                json={"amount": "1", "reason": "x"},
                                headers=hdr).status_code, (401, 403))

    # ============================================================
    # 6. RECONCILIATION WORKFLOW
    # ============================================================

    def test_06_reconciliation_workflow(self):
        rs, _k, _s = _make_reseller()
        _topup(rs["id"], 10000)
        db.cur.execute(
            "UPDATE reseller_wallet SET balance_cents=balance_cents+13"
            " WHERE reseller_id=?", (rs["id"],))
        db.conn.commit()

        ac = self._owner()
        hdr = {"X-CSRF-Token": "test-csrf"}
        r = ac.post("/api/reconciliation/run",
                    json={"reseller": rs["public_id"]}, headers=hdr)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["discrepancies_found"], 1)

        run_pub = r.get_json()["run_id"]
        d = ac.get(f"/api/reconciliation/runs/{run_pub}")
        self.assertEqual(d.status_code, 200)
        discs = d.get_json()["discrepancies"]
        self.assertEqual(discs[0]["type"], "balance_mismatch")
        self.assertEqual(discs[0]["severity"], "critical")

        dp = discs[0]["public_id"]
        self.assertEqual(
            ac.post(f"/api/reconciliation/discrepancies/{dp}/resolve",
                    json={"reason": ""}, headers=hdr).status_code, 400)
        non_owner = self._owner(role="admin")
        self.assertEqual(
            non_owner.post(f"/api/reconciliation/discrepancies/{dp}/resolve",
                           json={"reason": "x"}, headers=hdr).status_code, 403)
        ok = ac.post(f"/api/reconciliation/discrepancies/{dp}/resolve",
                     json={"reason": "investigated; corrected via ADJ",
                           "reference": "ADJ-FIX"}, headers=hdr)
        self.assertEqual(ok.get_json()["status"], "resolved")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 10013)

    # ============================================================
    # 7. WEBADMIN FINANCIAL CONTRACTS
    # ============================================================

    def test_07_webadmin_financial_contracts(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["F7-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15f-wa1")
        ac = self._owner()
        hdr = {"X-CSRF-Token": "test-csrf"}

        r = ac.get("/api/financial/overview")
        self.assertEqual(r.status_code, 200)
        o = r.get_json()["overview"]
        for k in ("total_wallet_balance", "sales_volume", "refund_volume",
                  "invoices", "statements", "reconciliation"):
            self.assertIn(k, o)
        self.assertEqual(o["currency"], "USDT")
        r = ac.get("/api/ledger?type=topup_credit")
        self.assertTrue(all(t["tx_type"] == "topup_credit"
                            for t in r.get_json()["transactions"]))
        r = ac.get("/api/adjustments")
        self.assertIn("adjustments", r.get_json())
        r = ac.get("/api/refunds")
        self.assertIn("refunds", r.get_json())
        today = _today()
        r = ac.post(f"/api/resellers/{rs['public_id']}/invoices/generate",
                    json={"period_start": today, "period_end": today},
                    headers=hdr)
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(r.get_json()["total"], "10.00")
        r = ac.post(f"/api/resellers/{rs['public_id']}/statements/generate",
                    json={"period_type": "custom", "start_date": today,
                          "end_date": today}, headers=hdr)
        self.assertTrue(r.get_json()["ok"])
        r = ac.post(f"/api/resellers/{rs['public_id']}/invoices/generate",
                    json={"period_start": "nope", "period_end": today},
                    headers=hdr)
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])
        self.assertNotIn("traceback", str(r.get_json()).lower())

    # ============================================================
    # 8. CROSS-FEATURE FINANCIAL CONSISTENCY (mandatory)
    # ============================================================

    def test_08_cross_feature_ledger_equality(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["F8-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 100000)
        oid = self._order(key, sec, pub, 1, "p15f-x1")
        issue_admin_refund(oid, 400, "capstone partial")

        today = _today()
        st = generate_statement(rs["id"], period_type="custom",
                                start_date=today, end_date=today)
        db.cur.execute(
            "SELECT tx_type, amount_cents FROM reseller_wallet_transactions"
            " WHERE reseller_id=?", (rs["id"],))
        rows = db.cur.fetchall()
        credits = sum(int(a) for t, a in rows if int(a) > 0)
        debits = sum(-int(a) for t, a in rows if int(a) < 0)
        self.assertEqual(st["total_credits_cents"], credits)
        self.assertEqual(st["total_debits_cents"], debits)
        self.assertEqual(st["purchases_cents"], 1000)
        self.assertEqual(st["refunds_cents"], 400)
        self.assertEqual(st["closing_balance_cents"], 100000 - 1000 + 400)
        self.assertEqual(st["closing_balance_cents"],
                         rdb.get_wallet_balance_cents(rs["id"]))
        self.assertEqual(st["orders_count"], 1)
        inv = generate_invoice(rs["id"], today, today)
        self.assertEqual(inv["subtotal_cents"], 1000)
        self.assertEqual(inv["refund_cents"], 400)
        self.assertEqual(inv["total_cents"], 600)
        self.assertEqual(reconcile_reseller(rs["id"]), [])

    # ============================================================
    # 9. PRIOR-PHASE REGRESSION FLOWS
    # ============================================================

    def test_09_prior_phase_regressions(self):
        # customer purchase at the master DB layer (repo convention:
        # handlers need python-telegram-bot; the DB flow is the contract)
        from utils.crypto import encrypt_text, decrypt_text
        pid, pub = _make_product(7.5, codes=["TG-1"])
        items = db.reserve_stock_items(pid, 555111222, 1)
        self.assertIsNotNone(items)
        blob = encrypt_text("\n\n".join(items))
        db.cur.execute(
            "INSERT INTO orders(user_id, product_name, price, delivery,"
            " product_id, payment_method) VALUES(?,?,?,?,?,?)",
            (555111222, "P15F Customer Product", 7.5, blob, pid, "crypto"))
        db.conn.commit()
        db.cur.execute("SELECT delivery FROM orders WHERE id=?", (db.cur.lastrowid,))
        self.assertIn("TG-1", decrypt_text(db.cur.fetchone()[0]))

        # reseller order with idempotent replay
        rs, key, sec = _make_reseller()
        pid2, pub2 = _make_product(10.0, codes=["RG-1"])
        _grant(rs["id"], pid2)
        _topup(rs["id"], 50000)
        r1 = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                              json={"product_id": pub2,
                                    "quantity": 1, "client_order_id": "p15f-replay"})
        self.assertEqual(r1.status_code, 201)
        r2 = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                              json={"product_id": pub2,
                                    "quantity": 1, "client_order_id": "p15f-replay"})
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.get_json()["data"]["idempotent"])
        self.assertEqual(r2.get_json()["data"]["order_id"],
                         r1.get_json()["data"]["order_id"])

        # webhook delivery through the real worker
        hook, _s = rdb.create_webhook(rs["id"], "https://capstone.example/h")
        from reseller_api.workers.webhook_worker import _process_event
        from reseller_api.services.webhook_outbox import get_outbox_row
        from reseller_api.services import webhook_delivery as wd
        db.cur.execute(
            "SELECT id FROM webhook_outbox WHERE reseller_id=?", (rs["id"],))
        event_ids = [r[0] for r in db.cur.fetchall()]
        self.assertTrue(event_ids)
        ok = {"success": True, "http_status": 200, "error_category": None,
              "responded_at": "t", "elapsed_ms": 3}
        with patch.object(wd, "_send_http", return_value=ok):
            for eid in event_ids:
                _process_event(get_outbox_row(eid))
        db.cur.execute(
            "SELECT COUNT(*) FROM webhook_outbox WHERE reseller_id=?"
            " AND status='delivered'", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), len(event_ids))

        # analytics equality
        wallet = rdb.get_wallet_financial_summary(rs["id"])
        sales = rdb.get_reseller_sales_analytics(rs["id"], days=30)
        self.assertEqual(wallet["total_purchases_cents"], sales["gross_sales_cents"])

    # ============================================================
    # 10. DOCUMENTATION SYNCHRONIZATION
    # ============================================================

    def test_10_documentation_synchronization(self):
        guide = (ROOT / "RESELLER_API.md").read_text()
        for token in ("invoices", "statements", "transactions",
                      "refund records", "INV-", "STMT-", "REF-"):
            self.assertIn(token, guide)
        rs, key, sec = _make_reseller()
        r = self.client.get("/api/v1", headers=_hdr(key, sec))
        resources = r.get_json()["data"]["available_resources"]
        for res in ("invoices", "statements", "transactions", "refunds"):
            self.assertIn(res, resources)


if __name__ == "__main__":
    unittest.main()
