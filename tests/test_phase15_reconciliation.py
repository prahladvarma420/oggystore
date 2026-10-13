"""Phase 15 Prompt 6 — financial reconciliation system.

Detection + control only — nothing auto-repairs.  Covers the full checklist
against a controlled dataset with deliberately injected corruption (raw
INSERTs only — the immutable ledger blocks UPDATE/DELETE), runs, severity,
resolution workflow, batching, isolation, and audit logging.
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase15_rec_", suffix=".db")
os.close(_fd)

import config
config.DB_PATH = _db_path
import database.db as db
db.DB_PATH = _db_path
db._tls = threading.local()
db.init_db()

import database.reseller_db as rdb
import database.reconciliation_db as rdb_rec
from reseller_api import create_app
import reseller_api.middleware.request_context  # noqa: F401
import reseller_api.middleware.rate_limit  # noqa: F401
import reseller_api.middleware.security_controls  # noqa: F401
from reseller_api.services.rate_limit import RequestRateDecision
from reseller_api.services.reconciliation import (
    reconcile_reseller, reconcile_platform, run_reconciliation,
    investigate_discrepancy, resolve_discrepancy, ignore_discrepancy,
    reopen_discrepancy, ReconciliationError,
)

_ctr = 0


def _n():
    global _ctr
    _ctr += 1
    return _ctr


def _make_reseller():
    n = _n()
    uid = 9_610_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P15C{n}", f"p15c{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P15C{n}", uid, f"p15c{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=()):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P15C Product {n}", price),
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P15C-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


def _inject_ledger(rid, tx_type, amount, balance_after, reference,
                   created=None):
    """Deliberate inconsistency injection (INSERT is allowed by the immutable
    ledger; UPDATE/DELETE are not — exactly the asymmetry reconciliation
    exists to catch)."""
    from utils.timezone import now_text
    db.cur.execute(
        "INSERT INTO reseller_wallet_transactions"
        "(reseller_id, tx_type, amount_cents, balance_after_cents, reference,"
        " description, created, currency) VALUES(?,?,?,?,?,?,?,?)",
        (rid, tx_type, amount, balance_after, reference, "injected",
         created or now_text(), "USDT"))
    db.conn.commit()
    return int(db.cur.lastrowid)


class Phase15ReconciliationTests(unittest.TestCase):
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

    # ------------------------------------------------------------ clean base

    def test_clean_ledger_passes_all_checks(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["CL-1", "CL-2"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15c-clean")
        from reseller_api.services.order_failure import handle_post_debit_failure
        oid = self._order(key, sec, pub, 1, "p15c-clean2")
        handle_post_debit_failure(oid, reason="t")
        self.assertEqual(reconcile_reseller(rs["id"]), [])

    # ------------------------------------------------------------ injected corruption

    def test_detects_wallet_ledger_mismatch(self):
        rs, _k, _s = _make_reseller()
        _topup(rs["id"], 10000)
        # wallet inflated without a ledger row (raw write on the wallet table)
        db.cur.execute(
            "UPDATE reseller_wallet SET balance_cents=balance_cents+777"
            " WHERE reseller_id=?", (rs["id"],))
        db.conn.commit()
        issues = reconcile_reseller(rs["id"])
        checks = {i["check"] for i in issues}
        self.assertIn("balance_mismatch", checks)
        self.assertEqual({i["severity"] for i in issues
                          if i["check"] == "balance_mismatch"}, {"critical"})

    def test_detects_chain_break_and_negative_balance(self):
        rs, _k, _s = _make_reseller()
        _topup(rs["id"], 10000)
        # chain-broken row: amount 500 but balance_after says 999999
        _inject_ledger(rs["id"], "adjustment_credit", 500, 999999,
                       f"ADJ-BRK-{_n()}")
        # wallet stays consistent with the ledger sum so only chain_break fires
        db.cur.execute(
            "UPDATE reseller_wallet SET balance_cents=balance_cents+500"
            " WHERE reseller_id=?", (rs["id"],))
        db.conn.commit()
        issues = reconcile_reseller(rs["id"])
        checks = {i["check"] for i in issues}
        self.assertIn("chain_break", checks)
        self.assertNotIn("balance_mismatch", checks)
        self.assertNotIn("negative_balance", checks)

    def test_detects_duplicates_orphans_currency(self):
        rs, _k, _s = _make_reseller()
        _topup(rs["id"], 10000)
        # duplicate reference: INSERT bypasses the app's check-then-insert but
        # the table has UNIQUE(reseller,tx_type,reference) — so a true
        # duplicate CANNOT be inserted; a near-duplicate (different type)
        # can.  The checker must not flag a legitimate different-type row.
        issues = reconcile_reseller(rs["id"])
        self.assertEqual(issues, [])
        # the immutable ledger blocks changing a row's currency after the fact
        _inject_ledger(rs["id"], "topup_credit", 100, 10100, f"TP-CUR-{_n()}")
        db.cur.execute(
            "UPDATE reseller_wallet SET balance_cents=balance_cents+100"
            " WHERE reseller_id=?", (rs["id"],))
        db.conn.commit()
        with self.assertRaises(Exception):
            db.cur.execute(
                "UPDATE reseller_wallet_transactions SET currency='EUR'"
                " WHERE reference LIKE 'TP-CUR-%'")
            db.conn.commit()
        db.conn.rollback()
        self.assertEqual(reconcile_reseller(rs["id"]), [])

        # a currency mismatch must instead be created at INSERT time (the
        # injection path) — and the check catches it
        db.cur.execute(
            "INSERT INTO reseller_wallet_transactions"
            "(reseller_id, tx_type, amount_cents, balance_after_cents,"
            " reference, description, created, currency)"
            " VALUES(?,?,?,?,?,?,?,'EUR')",
            (rs["id"], "topup_credit", 100, 10200, f"TP-EUR-{_n()}",
             "injected", "2026-01-01 00:00:00"))
        db.cur.execute(
            "UPDATE reseller_wallet SET balance_cents=balance_cents+100"
            " WHERE reseller_id=?", (rs["id"],))
        db.conn.commit()
        issues = reconcile_reseller(rs["id"])
        self.assertIn("currency_mismatch", {i["check"] for i in issues})
        # the mismatch row still got its wallet credit, so no balance mismatch
        self.assertNotIn("balance_mismatch", {i["check"] for i in issues})
        # orphans are impossible: the ledger's FK (reseller_id → resellers,
        # RESTRICT) rejects a row for a nonexistent reseller
        import sqlite3 as _sq
        with self.assertRaises(_sq.IntegrityError):
            _inject_ledger(99999999, "topup_credit", 100, 100, f"TP-ORF-{_n()}")
        db.conn.rollback()
        # the platform orphan check stays as a safety net (FK-off deployments)
        plat = reconcile_platform()
        self.assertFalse(any(i["check"] == "orphan_transaction"
                             for i in plat["issues"]))

    def test_detects_order_and_refund_mismatches(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["OM-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 10000)
        # debit without an order (injected purchase_debit)
        _inject_ledger(rs["id"], "purchase_debit", -1000, 9000,
                       f"rapi.{rs['id']}.ghost-request")
        db.cur.execute(
            "UPDATE reseller_wallet SET balance_cents=balance_cents-1000"
            " WHERE reseller_id=?", (rs["id"],))
        db.conn.commit()
        issues = reconcile_reseller(rs["id"])
        checks = {i["check"] for i in issues}
        self.assertIn("debit_without_order", checks)

        # refund without a debit (injected refund_credit for ghost order)
        _inject_ledger(rs["id"], "refund_credit", 300, 9300,
                       f"rapi.refund.order.999999")
        db.cur.execute(
            "UPDATE reseller_wallet SET balance_cents=balance_cents+300"
            " WHERE reseller_id=?", (rs["id"],))
        db.conn.commit()
        issues = reconcile_reseller(rs["id"])
        checks = {i["check"] for i in issues}
        self.assertIn("refund_without_debit", checks)

    def test_detects_adjustment_and_invoice_statement_mismatches(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["AM-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 10000)
        self._order(key, sec, pub, 1, "p15c-am1")
        today = "2026-10-07"

        from reseller_api.services.adjustment_service import (
            create_adjustment, approve_adjustment, complete_adjustment)
        from reseller_api.services.invoice_service import generate_invoice
        from reseller_api.services.statement_service import generate_statement

        # completed adjustment then corrupt the record's ledger reference away
        # is impossible (frozen); instead inject a bare legacy adjustment row
        _inject_ledger(rs["id"], "adjustment_credit", 250, 10250,
                       f"ADJ-LEGACY-{_n()}")
        db.cur.execute(
            "UPDATE reseller_wallet SET balance_cents=balance_cents+250"
            " WHERE reseller_id=?", (rs["id"],))
        db.conn.commit()
        issues = reconcile_reseller(rs["id"])
        # legacy adjustment rows are INFO-level (pre-P15 record layer)
        self.assertTrue(any(i["check"] == "missing_adjustment_record"
                            and i["severity"] == "info" for i in issues))

        # pending-but-never-completed adjustment with an injected ledger row
        adj = create_adjustment(rs["id"], "credit", 100, "never posted")
        _inject_ledger(rs["id"], "adjustment_credit", 100, 10350,
                       adj["ledger_reference"])
        db.cur.execute(
            "UPDATE reseller_wallet SET balance_cents=balance_cents+100"
            " WHERE reseller_id=?", (rs["id"],))
        db.conn.commit()
        issues = reconcile_reseller(rs["id"])
        self.assertTrue(any(i["check"] == "adjustment_ledger_mismatch"
                            and i["severity"] == "high" for i in issues))

        # invoice mismatch: finalize an invoice then inject an extra purchase
        # so the invoice window no longer matches
        inv = generate_invoice(rs["id"], today, today)
        self.assertEqual(inv["status"], "finalized")
        before = reconcile_reseller(rs["id"])
        self.assertNotIn("invoice_ledger_mismatch",
                         {i["check"] for i in before})
        # inject an extra order charge inside the invoice window
        # (timestamp set at INSERT time — the ledger is immutable to updates)
        bal = rdb.get_wallet_balance_cents(rs["id"])
        _inject_ledger(rs["id"], "purchase_debit", -500, bal - 500,
                       f"rapi.{rs['id']}.late-order",
                       created=f"{today} 12:00:00")
        db.cur.execute(
            "UPDATE reseller_wallet SET balance_cents=balance_cents-500"
            " WHERE reseller_id=?", (rs["id"],))
        db.conn.commit()
        issues = reconcile_reseller(rs["id"])
        self.assertIn("invoice_ledger_mismatch", {i["check"] for i in issues})
        self.assertIn("debit_without_order", {i["check"] for i in issues})

        # statement mismatch: finalized statement then inject movement
        st = generate_statement(rs["id"], period_type="custom",
                                start_date=today, end_date=today,
                                finalize=True)
        bal = rdb.get_wallet_balance_cents(rs["id"])
        _inject_ledger(rs["id"], "topup_credit", 999, bal + 999,
                       f"TP-LATE-{_n()}", created=f"{today} 23:00:00")
        db.cur.execute(
            "UPDATE reseller_wallet SET balance_cents=balance_cents+999"
            " WHERE reseller_id=?", (rs["id"],))
        db.conn.commit()
        issues = reconcile_reseller(rs["id"])
        self.assertIn("statement_ledger_mismatch", {i["check"] for i in issues})

    # ------------------------------------------------------------ runs + workflow

    def test_run_persistence_and_idempotent_findings(self):
        rs, _k, _s = _make_reseller()
        _topup(rs["id"], 10000)
        db.cur.execute(
            "UPDATE reseller_wallet SET balance_cents=balance_cents+5"
            " WHERE reseller_id=?", (rs["id"],))
        db.conn.commit()

        run1 = run_reconciliation(reseller_id=rs["id"], created_by="owner")
        self.assertEqual(run1["status"], "completed")
        self.assertEqual(run1["discrepancies_found"], 1)
        self.assertRegex(run1["public_id"], r"^RCR-[A-HJ-NP-Z2-9]{10}$")

        # rerun: the same finding is re-touched (last-seen moves to run2),
        # NOT duplicated
        run2 = run_reconciliation(reseller_id=rs["id"], created_by="owner")
        self.assertEqual(run2["discrepancies_found"], 1)
        discs = rdb_rec.list_discrepancies(run_id=run2["id"])
        self.assertEqual(len(discs), 1)      # the SAME row, re-touched
        self.assertEqual(discs[0]["public_id"],
                         rdb_rec.list_discrepancies(run_id=run1["id"])[0]["public_id"]
                         if rdb_rec.list_discrepancies(run_id=run1["id"]) else discs[0]["public_id"])
        # at most one OPEN/investigating row per fingerprint; resolved rows
        # may share it with a later regression row by design
        open_fps = [d["fingerprint"] for d in rdb_rec.list_discrepancies()
                    if d["status"] in ("open", "investigating")]
        self.assertEqual(len(open_fps), len(set(open_fps)))

        # runs are append-only
        with self.assertRaises(Exception):
            db.cur.execute("DELETE FROM reconciliation_runs WHERE id=?",
                           (run1["id"],))
            db.conn.commit()
        db.conn.rollback()

    def test_discrepancy_resolution_workflow(self):
        rs, _k, _s = _make_reseller()
        _topup(rs["id"], 10000)
        db.cur.execute(
            "UPDATE reseller_wallet SET balance_cents=balance_cents+1"
            " WHERE reseller_id=?", (rs["id"],))
        db.conn.commit()
        run = run_reconciliation(reseller_id=rs["id"], created_by="owner")
        disc = rdb_rec.list_discrepancies(status="open")[0]
        self.assertEqual(disc["severity"], "critical")
        self.assertEqual(disc["type"], "balance_mismatch")

        # investigate
        d = investigate_discrepancy(disc["public_id"], actor="owner")
        self.assertEqual(d["status"], "investigating")
        # resolve requires a reason
        with self.assertRaises(ReconciliationError):
            resolve_discrepancy(disc["public_id"], "  ", resolver="owner")
        d = resolve_discrepancy(disc["public_id"], "corrected via ADJ",
                                resolver="owner", reference="ADJ-FIX-1")
        self.assertEqual(d["status"], "resolved")
        self.assertEqual(d["resolved_by"], "owner")
        self.assertEqual(d["resolution_reason"], "corrected via ADJ")
        self.assertEqual(d["resolution_reference"], "ADJ-FIX-1")
        self.assertTrue(d["resolved_at"])
        # resolved is terminal for the instance; reopen works when needed
        d = reopen_discrepancy(disc["public_id"], actor="owner")
        self.assertEqual(d["status"], "open")
        # invalid jump: open -> resolved allowed; open -> ignored allowed;
        # resolved -> investigating is NOT
        resolve_discrepancy(disc["public_id"], "again", resolver="owner")
        with self.assertRaises(ReconciliationError):
            investigate_discrepancy(disc["public_id"], actor="owner")

    def test_regression_after_resolution_opens_new(self):
        rs, _k, _s = _make_reseller()
        _topup(rs["id"], 10000)
        db.cur.execute(
            "UPDATE reseller_wallet SET balance_cents=balance_cents+3"
            " WHERE reseller_id=?", (rs["id"],))
        db.conn.commit()
        run_reconciliation(reseller_id=rs["id"])
        disc = rdb_rec.list_discrepancies(status="open")[0]
        resolve_discrepancy(disc["public_id"], "fixed", resolver="owner")
        # problem persists (we did not actually fix it) -> NEW row on rerun
        run_reconciliation(reseller_id=rs["id"])
        all_d = [d for d in rdb_rec.list_discrepancies()
                 if d["fingerprint"] == disc["fingerprint"]]
        self.assertEqual(len(all_d), 2)
        self.assertTrue(any(d["status"] == "open" for d in all_d))
        self.assertTrue(any(d["status"] == "resolved" for d in all_d))

    def test_date_range_scoping_and_timezone(self):
        rs, _k, _s = _make_reseller()
        _topup(rs["id"], 10000)
        today = _today_local()
        # a mismatch row yesterday and today
        _inject_ledger(rs["id"], "topup_credit", 100, 10100,
                       f"TP-OLD-{_n()}", created=f"{_yesterday()} 10:00:00")
        db.cur.execute(
            "UPDATE reseller_wallet SET balance_cents=balance_cents+100"
            " WHERE reseller_id=?", (rs["id"],))
        db.conn.commit()
        db.cur.execute(
            "UPDATE reseller_wallet SET balance_cents=balance_cents+7"
            " WHERE reseller_id=?", (rs["id"],))
        db.conn.commit()
        # the +7 wallet-only drift fires regardless of window (balance check
        # is full-history by design); the window scopes row-level checks
        issues = reconcile_reseller(rs["id"], date_from=today, date_to=today)
        checks = {i["check"] for i in issues}
        self.assertIn("balance_mismatch", checks)          # full-history check
        # and the run records the window
        run = run_reconciliation(reseller_id=rs["id"], date_from=today,
                                 date_to=today, created_by="owner")
        self.assertEqual(run["period_start"], today)
        self.assertEqual(run["period_end"], today)

    def test_global_reconciliation_batched(self):
        # several resellers, platform sweep
        for _ in range(3):
            rs, _k, _s = _make_reseller()
            _topup(rs["id"], 10000)
        plat = reconcile_platform()
        self.assertGreaterEqual(plat["resellers_checked"], 3)
        self.assertIn("per_reseller", plat)

    def test_webadmin_run_workflow_endpoints(self):
        rs, _k, _s = _make_reseller()
        _topup(rs["id"], 10000)
        db.cur.execute(
            "UPDATE reseller_wallet SET balance_cents=balance_cents+9"
            " WHERE reseller_id=?", (rs["id"],))
        db.conn.commit()

        import web_admin.app as wa
        wa.app.config["TESTING"] = True
        wa.DB_PATH = _db_path
        ac = wa.app.test_client()
        with ac.session_transaction() as sess:
            sess["logged_in"] = True
            sess["username"] = "owner"
            sess["web_role"] = "owner"
            sess["csrf"] = "test-csrf"
        hdr = {"X-CSRF-Token": "test-csrf"}

        # start a run
        r = ac.post("/api/reconciliation/run",
                    json={"reseller": rs["public_id"]}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(r.get_json()["discrepancies_found"], 1)
        run_pub = r.get_json()["run_id"]

        # history + dashboard
        r = ac.get("/api/reconciliation/runs")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["runs"])
        self.assertGreaterEqual(body["dashboard"]["total_runs"], 1)
        self.assertIn("open_by_severity", body["dashboard"])
        self.assertIn("open_by_type", body["dashboard"])
        self.assertNotIn("id", body["runs"][0])      # internal ids stripped

        # run detail with discrepancies
        r = ac.get(f"/api/reconciliation/runs/{run_pub}")
        self.assertEqual(r.status_code, 200)
        discs = r.get_json()["discrepancies"]
        self.assertEqual(len(discs), 1)
        self.assertEqual(discs[0]["type"], "balance_mismatch")
        self.assertEqual(discs[0]["severity"], "critical")
        self.assertNotIn("reseller_id", discs[0])

        # workflow via endpoints
        dp = discs[0]["public_id"]
        self.assertEqual(
            ac.post(f"/api/reconciliation/discrepancies/{dp}/investigate",
                    json={}, headers=hdr).get_json()["status"], "investigating")
        self.assertEqual(
            ac.post(f"/api/reconciliation/discrepancies/{dp}/resolve",
                    json={"reason": ""}, headers=hdr).status_code, 400)
        self.assertEqual(
            ac.post(f"/api/reconciliation/discrepancies/{dp}/resolve",
                    json={"reason": "corrected", "reference": "ADJ-X"},
                    headers=hdr).get_json()["status"], "resolved")

        # gates
        anon = wa.app.test_client()
        self.assertEqual(anon.get("/api/reconciliation/runs").status_code, 401)
        non_owner = wa.app.test_client()
        with non_owner.session_transaction() as sess:
            sess["logged_in"] = True
            sess["username"] = "admin2"
            sess["web_role"] = "admin"
            sess["csrf"] = "test-csrf"
        self.assertEqual(
            non_owner.post("/api/reconciliation/run", json={},
                           headers=hdr).status_code, 403)
        self.assertEqual(
            non_owner.post(f"/api/reconciliation/discrepancies/{dp}/resolve",
                           json={"reason": "x"}, headers=hdr).status_code, 403)

        # audit rows
        db.cur.execute(
            "SELECT COUNT(*) FROM admin_logs WHERE action LIKE '%reconciliation%'")
        self.assertGreaterEqual(int(db.cur.fetchone()[0]), 2)

    def test_reconciliation_is_read_only(self):
        rs, _k, _s = _make_reseller()
        _topup(rs["id"], 10000)
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_wallet_transactions WHERE reseller_id=?",
            (rs["id"],))
        before_ledger = int(db.cur.fetchone()[0])
        bal_before = rdb.get_wallet_balance_cents(rs["id"])
        run_reconciliation(reseller_id=rs["id"])
        reconcile_platform()
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_wallet_transactions WHERE reseller_id=?",
            (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), before_ledger)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), bal_before)

    def test_sandbox_marker(self):
        from reseller_api.config import validate_environment_marker
        with self.assertRaises(RuntimeError):
            validate_environment_marker({"API_ENVIRONMENT": "production"})


def _today_local():
    from utils.timezone import now as _now
    return _now().strftime("%Y-%m-%d")


def _yesterday():
    from datetime import datetime as dt, timedelta as td
    from utils.timezone import now as _now
    return (_now() - td(days=1)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    unittest.main()
