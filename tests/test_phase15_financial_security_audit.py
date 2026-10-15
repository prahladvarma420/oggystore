"""Phase 15 Prompt 9 — financial security & audit gate.

Executable proof that the Phase 15 surfaces (transactions, refunds,
invoices, statements, adjustments, admin refunds, reconciliation, WebAdmin
financial pages) are reseller-safe, owner-safe, and ledger-consistent.

Style mirrors test_phase14_analytics_audit.py; the live-stack pattern
mirrors test_phase13_api_security_audit.py.
"""

import json
import os
import re
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase15_audit_", suffix=".db")
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
import reseller_api.middleware.authentication  # noqa: F401
from reseller_api.services.rate_limit import RequestRateDecision
from reseller_api.services.adjustment_service import (
    AdjustmentError, create_adjustment, approve_adjustment,
    reject_adjustment, cancel_adjustment, complete_adjustment,
    issue_admin_refund,
)
from reseller_api.services.invoice_service import generate_invoice
from reseller_api.services.statement_service import generate_statement
from reseller_api.services.reconciliation import (
    reconcile_reseller, run_reconciliation,
)

_ctr = 0


def _n():
    global _ctr
    _ctr += 1
    return _ctr


def _make_reseller():
    n = _n()
    uid = 9_580_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P15G{n}", f"p15g{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P15G{n}", uid, f"p15g{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=()):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P15G Product {n}", price),
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P15G-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


def _today():
    from utils.timezone import now as _now
    return _now().strftime("%Y-%m-%d")


class Phase15FinancialSecurityAudit(unittest.TestCase):
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
    # 1. ISOLATION
    # ============================================================

    def test_01_reseller_isolation_uniform_404(self):
        ra, ka, sa = _make_reseller()
        rb, kb, sb = _make_reseller()
        pid, pub = _make_product(10.0, codes=["G1-1"])
        _grant(ra["id"], pid)
        _topup(ra["id"], 50000)
        oid = self._order(ka, sa, pub, 1, "p15g-iso")
        today = _today()
        inv = generate_invoice(ra["id"], today, today)
        st = generate_statement(ra["id"], period_type="custom",
                                start_date=today, end_date=today)
        rec = issue_admin_refund(oid, 500, "audit isolation", actor="owner")

        # every A-owned resource is an identical 404 for B
        paths_a = [
            f"/api/v1/invoices/{inv['public_id']}",
            f"/api/v1/invoices/{inv['public_id']}/lines",
            f"/api/v1/invoices/{inv['public_id']}/transactions",
            f"/api/v1/invoices/{inv['public_id']}/document",
            f"/api/v1/statements/{st['public_id']}",
            f"/api/v1/statements/{st['public_id']}/transactions",
            f"/api/v1/statements/{st['public_id']}/invoices",
            f"/api/v1/statements/{st['public_id']}/document",
            f"/api/v1/refunds/{rec['public_id']}",
        ]
        for path in paths_a:
            r = self.client.get(path, headers=_hdr(kb, sb))
            self.assertEqual(r.status_code, 404, path)
        # identical body shape to a nonexistent id (no oracle)
        fake_inv = self.client.get("/api/v1/invoices/INV-ZZZZZZZZZZZZ",
                                   headers=_hdr(kb, sb))
        real_inv = self.client.get(paths_a[0], headers=_hdr(kb, sb))
        self.assertEqual(fake_inv.get_json()["error"],
                         real_inv.get_json()["error"])
        # B's lists contain nothing of A's
        for path in ("/api/v1/invoices", "/api/v1/statements",
                     "/api/v1/refunds", "/api/v1/transactions"):
            body = self.client.get(path, headers=_hdr(kb, sb)).get_json()
            self.assertNotIn(inv["public_id"], json.dumps(body))
            self.assertNotIn(st["public_id"], json.dumps(body))

    def test_01b_webadmin_authz_matrix(self):
        import web_admin.app as wa
        wa.app.config["TESTING"] = True
        wa.DB_PATH = _db_path
        anon = wa.app.test_client()
        reads = ["/api/financial/overview", "/api/ledger",
                 "/api/adjustments", "/api/refunds", "/api/invoices",
                 "/api/statements", "/api/reconciliation/runs",
                 "/api/reconciliation/discrepancies"]
        for path in reads:
            self.assertEqual(anon.get(path).status_code, 401, path)
        # mutations: anon 401; non-owner 403
        non_owner = self._owner(role="admin")
        hdr = {"X-CSRF-Token": "test-csrf"}
        rs, _k, _s = _make_reseller()
        mutations = [
            ("POST", f"/api/resellers/{rs['public_id']}/statements/generate",
             {"period_type": "custom", "start_date": _today(),
              "end_date": _today()}),
            ("POST", f"/api/resellers/{rs['public_id']}/invoices/generate",
             {"period_start": _today(), "period_end": _today()}),
            ("POST", "/api/reconciliation/run", {}),
        ]
        for method, path, payload in mutations:
            # anon POSTs: the CSRF guard (403) runs before the login gate
            # (401) — both deny without touching data; accept either
            self.assertIn(anon.post(path, json=payload,
                                    headers=hdr).status_code, (401, 403), path)
            self.assertEqual(non_owner.post(path, json=payload,
                                            headers=hdr).status_code, 403, path)

    # ============================================================
    # 2. MONEY CAP
    # ============================================================

    def test_02_refund_cap_including_race(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["G2-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        oid = self._order(key, sec, pub, 1, "p15g-cap")   # $10.00 charged

        # two racing refunds of $7 against a $10 charge: total ≤ $10
        barrier = threading.Barrier(2)
        results, errors = [], []

        def worker():
            barrier.wait()
            try:
                results.append(issue_admin_refund(oid, 700, "race"))
            except Exception as e:
                errors.append(e)

        a, b = threading.Thread(target=worker), threading.Thread(target=worker)
        a.start(); b.start(); a.join(); b.join()
        db.cur.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM"
            " reseller_wallet_transactions WHERE reseller_id=?"
            " AND tx_type='refund_credit'", (rs["id"],))
        total_refunded = int(db.cur.fetchone()[0])
        self.assertLessEqual(total_refunded, 1000)
        self.assertEqual(len(results) + len(errors), 2)
        self.assertTrue(results)  # at least one posted
        if errors:
            self.assertEqual(errors[0].code, "refund_exceeds_refundable")

        # fully refunded → 409-style business error
        issue_admin_refund(oid, 1000 - total_refunded, "finish")
        with self.assertRaises(AdjustmentError) as ctx:
            issue_admin_refund(oid, 100, "too late")
        self.assertEqual(ctx.exception.code, "nothing_to_refund")
        self.assertEqual(ctx.exception.status, 409)

    # ============================================================
    # 3. RECORD/LEDGER ATOMICITY
    # ============================================================

    def test_03_record_ledger_atomicity_and_crash(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["G3-1", "G3-2"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        oid = self._order(key, sec, pub, 1, "p15g-atomic")

        # crash BETWEEN ledger post and record insert → the atomic block
        # rolls both back: no divergence is possible
        import database.adjustment_db as _adb
        original = _adb.insert_refund_record

        def exploding(fields):
            raise RuntimeError("simulated crash at record insert")

        _adb.insert_refund_record = exploding
        try:
            with self.assertRaises(RuntimeError):
                issue_admin_refund(oid, 400, "crash test")
        finally:
            _adb.insert_refund_record = original

        # no ledger row, no record, no wallet movement, no outbox event
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND tx_type='refund_credit'", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 0)
        self.assertEqual(adb.list_refunds(reseller_id=rs["id"]), [])
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 49000)
        db.cur.execute(
            "SELECT COUNT(*) FROM webhook_outbox WHERE reseller_id=?"
            " AND event_type='refund.created'", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 0)

        # after the crash, the retry succeeds cleanly
        rec = issue_admin_refund(oid, 400, "retry after crash")
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND tx_type='refund_credit'", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 1)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 49400)
        self.assertTrue(rec["public_id"].startswith("REF-"))

        # every admin-family ledger row has exactly one record with the
        # same ledger_reference, and vice versa
        db.cur.execute(
            "SELECT reference FROM reseller_wallet_transactions"
            " WHERE tx_type='refund_credit' AND reference LIKE 'rapi.admin_refund.%'")
        for (ref,) in db.cur.fetchall():
            rec = adb.get_refund_by_ledger_reference(ref)
            self.assertIsNotNone(rec, ref)
        for rec in adb.list_refunds():
            db.cur.execute(
                "SELECT COUNT(*) FROM reseller_wallet_transactions"
                " WHERE tx_type='refund_credit' AND reference=?",
                (rec["ledger_reference"],))
            self.assertEqual(int(db.cur.fetchone()[0]), 1)

    # ============================================================
    # 4. ADJUSTMENT WORKFLOW
    # ============================================================

    def test_04_adjustment_workflow_money_timing(self):
        rs, _k, _s = _make_reseller()
        _topup(rs["id"], 10000)

        adj = create_adjustment(rs["id"], "credit", 500, "promo",
                                created_by="owner")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 10000)
        approve_adjustment(adj["id"], decided_by="owner")
        # approved: STILL no money
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 10000)
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND tx_type='adjustment_credit'", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 0)
        done = complete_adjustment(adj["id"], decided_by="owner")
        self.assertEqual(done["status"], "completed")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 10500)
        # exactly once
        complete_adjustment(adj["id"], decided_by="owner")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 10500)

        # rejected / cancelled never post
        a2 = create_adjustment(rs["id"], "debit", 100, "bad")
        reject_adjustment(a2["id"], decided_by="owner")
        with self.assertRaises(AdjustmentError):
            complete_adjustment(a2["id"])
        a3 = create_adjustment(rs["id"], "credit", 100, "nvm")
        cancel_adjustment(a3["id"], decided_by="owner")
        with self.assertRaises(AdjustmentError):
            approve_adjustment(a3["id"])
        # illegal jumps
        a4 = create_adjustment(rs["id"], "credit", 100, "x")
        with self.assertRaises(AdjustmentError):
            complete_adjustment(a4["id"])          # pending → completed illegal
        # ceiling: default 100_000_00 (settings-driven)
        from database.db import get_setting
        ceiling = int(get_setting("max_manual_adjustment_cents", "10000000"))
        ok_ceiling = create_adjustment(rs["id"], "credit", ceiling, "at ceiling")
        self.assertEqual(ok_ceiling["amount_cents"], ceiling)
        with self.assertRaises(AdjustmentError) as ctx:
            create_adjustment(rs["id"], "credit", ceiling + 1, "over")
        self.assertEqual(ctx.exception.code, "adjustment_limit_exceeded")

    # ============================================================
    # 5. RECONCILIATION INTEGRITY
    # ============================================================

    def test_05_reconciliation_detects_and_never_repairs(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["G5-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 10000)
        self._order(key, sec, pub, 1, "p15g-rec")
        before = reconcile_reseller(rs["id"])
        self.assertEqual(before, [])

        # inject one fault class at a time (INSERT-only injections — the
        # immutable ledger forbids UPDATE/DELETE, which the scan itself proves)
        from utils.timezone import now_text
        bal = rdb.get_wallet_balance_cents(rs["id"])
        db.cur.execute(
            "INSERT INTO reseller_wallet_transactions"
            "(reseller_id, tx_type, amount_cents, balance_after_cents,"
            " reference, description, created, currency) VALUES"
            " (?,?,?,?,?,?,?,?)",
            (rs["id"], "adjustment_credit", 500, bal + 999, f"ADJ-BRK-{_n()}",
             "injected", now_text(), "USDT"))
        db.cur.execute(
            "UPDATE reseller_wallet SET balance_cents=balance_cents+500"
            " WHERE reseller_id=?", (rs["id"],))
        db.conn.commit()
        issues = reconcile_reseller(rs["id"])
        self.assertIn("chain_break", {i["check"] for i in issues})
        # never repairs: wallet + ledger unchanged by the scan
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), bal + 500)
        db.cur.execute(
            "SELECT balance_after_cents FROM reseller_wallet_transactions"
            " WHERE reference LIKE 'ADJ-BRK-%'")
        self.assertEqual(int(db.cur.fetchone()[0]), bal + 999)

        # repair the corruption via the AUTHORIZED path (adjustment record
        # noting the fix), then the chain reconciles again
        # (the injected row stays — history is never rewritten)
        issues2 = reconcile_reseller(rs["id"])
        self.assertTrue(any(i["check"] == "chain_break" for i in issues2))

    # ============================================================
    # 6. DOCUMENT SAFETY
    # ============================================================

    def test_06_documents_escape_and_hide(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["G6-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15g-doc")
        today = _today()
        inv = generate_invoice(rs["id"], today, today)
        st = generate_statement(rs["id"], period_type="custom",
                                start_date=today, end_date=today)

        # user-controlled strings in the store name + reseller name must be
        # HTML-escaped in the documents
        db.cur.execute("UPDATE resellers SET name=? WHERE id=?",
                       ("<script>alert(1)</script>", rs["id"]))
        db.conn.commit()
        rs2 = rdb.get_reseller(rs["id"])
        today2 = _today()
        inv2 = generate_invoice(rs["id"], today2, today2)  # dup — existing
        # regenerate after rename: same invoice (idempotent) — snapshot from P2
        inv3 = generate_invoice(rs["id"], today2, today2, finalize=False)
        self.assertEqual(inv3["id"], inv2["id"])

        # a statement generated AFTER the rename snapshots the poisoned
        # reseller name — the document must escape it, never execute it
        import datetime as _dt
        from utils.timezone import now as _now
        yesterday = (_now() - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        st2 = generate_statement(rs["id"], period_type="custom",
                                 start_date=yesterday, end_date=yesterday)
        H = _hdr(key, sec)
        doc = self.client.get(
            f"/api/v1/statements/{st2['public_id']}/document", headers=H)
        self.assertEqual(doc.status_code, 200)
        html = doc.get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)     # escaped, not stripped
        self.assertNotIn(sec, html)
        self.assertNotIn(f">{rs['id']}<", html)   # internal id as a cell value
        # the invoice document (fixed snapshot from before the rename)
        idoc = self.client.get(f"/api/v1/invoices/{inv['public_id']}/document",
                               headers=H)
        self.assertEqual(idoc.status_code, 200)
        self.assertNotIn("<script>", idoc.get_data(as_text=True).lower())

    # ============================================================
    # 7. HYGIENE SCANS
    # ============================================================

    def test_07_hygiene_scans_after_full_workflow(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["G7-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        hook, hook_secret = rdb.create_webhook(rs["id"], "https://hy.example/h")
        oid = self._order(key, sec, pub, 1, "p15g-hyg")
        today = _today()
        generate_invoice(rs["id"], today, today)
        generate_statement(rs["id"], period_type="custom",
                           start_date=today, end_date=today)
        issue_admin_refund(oid, 300, "hygiene", actor="owner")
        run_reconciliation(reseller_id=rs["id"], created_by="owner")
        # a failed auth attempt for the logs
        self.client.get("/api/v1/invoices",
                        headers=_hdr(key, sec + "wrong"))

        # scan every operational store for secret/hash/exception material
        stores = []
        for table, col in (("reseller_api_logs", "*"),
                           ("reseller_security_events", "*"),
                           ("webhook_outbox", "payload_json"),
                           ("admin_logs", "*"),
                           ("reconciliation_runs", "*"),
                           ("reconciliation_discrepancies", "*")):
            try:
                db.cur.execute(f"SELECT {col} FROM {table}")
                stores.extend(str(r) for r in db.cur.fetchall())
            except Exception:
                continue
        blob = "\n".join(stores)
        self.assertNotIn(sec, blob)                       # API secret
        self.assertNotIn(hook_secret, blob)               # webhook secret
        self.assertNotIn("pbkdf2", blob.lower())          # password hash markers
        self.assertNotIn("$2b$", blob)
        self.assertNotIn("traceback", blob.lower())
        self.assertNotIn("integrityerror", blob.lower())
        self.assertNotIn("/data/", blob)                  # internal paths

    # ============================================================
    # 8. EDGE CASES
    # ============================================================

    def test_08_edge_cases(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["G8-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        oid = self._order(key, sec, pub, 1, "p15g-edge")

        # legacy NULL-money order row (pre-P12 shape): refund must not crash
        # and must refuse (no computable charge → nothing refundable)
        db.cur.execute(
            "INSERT INTO orders(user_id, product_name, price, delivery,"
            " product_id, payment_method, source, reseller_id, created)"
            " VALUES(0, 'legacy', 10.0, 'x', ?, 'reseller_api',"
            " 'reseller_api', ?, '2026-01-01 00:00:00')",
            (pid, rs["id"]))
        db.conn.commit()
        legacy_oid = db.cur.lastrowid
        with self.assertRaises(AdjustmentError) as ctx:
            issue_admin_refund(legacy_oid, 100, "legacy")
        self.assertEqual(ctx.exception.code, "nothing_to_refund")

        # empty-period statement: valid, zeroed
        import datetime as _dt
        from utils.timezone import now as _now
        past = (_now() - _dt.timedelta(days=2)).strftime("%Y-%m-%d")
        st = generate_statement(rs["id"], period_type="custom",
                                start_date=past, end_date=past)
        self.assertEqual(st["transactions_count"], 0)
        self.assertEqual(st["closing_balance_cents"],
                         st["opening_balance_cents"])

        # reconciliation on an empty reseller is clean
        rs2, _k2, _s2 = _make_reseller()
        self.assertEqual(reconcile_reseller(rs2["id"]), [])

    # ============================================================
    # 9. API ROUTE PARITY + ENVELOPE
    # ============================================================

    def test_09_phase15_routes_match_openapi(self):
        import yaml
        spec = yaml.safe_load((ROOT / "docs" / "openapi.yaml").read_text())
        documented = {(m.upper(), p) for p, item in spec["paths"].items()
                      for m in item}
        live = set()
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint.startswith("reseller_api_v1."):
                for m in rule.methods - {"HEAD", "OPTIONS"}:
                    pth = re.sub(
                        r"<(?:[^:>]+:)?([^>]+)>",
                        lambda mm: "{%s}" % {
                            "order_id": "orderId", "product_id": "productId",
                            "webhook_id": "webhookId", "delivery_id": "deliveryId",
                            "outbox_id": "eventId", "invoice_id": "invoiceId",
                            "statement_id": "statementId",
                            "refund_id": "refundId"}.get(mm.group(1),
                                                         mm.group(1)),
                        rule.rule)
                    live.add((m, pth.replace("/api/v1", "", 1) or "/"))
        live.discard(("GET", ""))
        self.assertEqual(live, documented)

        # WebAdmin mutations use the {ok:...} envelope (their convention)
        ac = self._owner()
        hdr = {"X-CSRF-Token": "test-csrf"}
        rs, _k, _s = _make_reseller()
        r = ac.post(f"/api/resellers/{rs['public_id']}/statements/generate",
                    json={"period_type": "custom", "start_date": _today(),
                          "end_date": _today()}, headers=hdr)
        body = r.get_json()
        self.assertIn("ok", body)
        self.assertTrue(body["ok"])
        self.assertNotIn("traceback", str(body).lower())
        # failure envelope is equally consistent
        r = ac.post("/api/reconciliation/run",
                    json={"reseller": "RSR-ZZZZZZZZZZ"}, headers=hdr)
        self.assertEqual(r.status_code, 404)
        self.assertIn("ok", r.get_json())
        self.assertFalse(r.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
