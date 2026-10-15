"""Phase 15 Prompt 5 — refunds, credits & financial adjustments framework.

Covers the full checklist against the real stack: refund types and records,
validation, multiple/partial refunds, the outstanding cap, concurrency,
idempotency, the adjustment approval workflow, limits, admin authorization,
invoice/statement/analytics integration, reversal-by-compensation,
reconciliation, isolation, sandbox, and the controlled financial scenario
with cross-surface agreement.
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase15_adj_", suffix=".db")
os.close(_fd)

import config
config.DB_PATH = _db_path
import database.db as db
db.DB_PATH = _db_path
db._tls = threading.local()
db.init_db()

import database.reseller_db as rdb
import database.adjustment_db as adb
from reseller_api import create_app
import reseller_api.middleware.request_context  # noqa: F401
import reseller_api.middleware.rate_limit  # noqa: F401
import reseller_api.middleware.security_controls  # noqa: F401
from reseller_api.services.rate_limit import RequestRateDecision
from reseller_api.services.adjustment_service import (
    AdjustmentError, create_adjustment, approve_adjustment,
    reject_adjustment, cancel_adjustment, complete_adjustment,
    issue_admin_refund,
)
import database.invoice_db as idb
from reseller_api.services.invoice_service import generate_invoice
from reseller_api.services.order_failure import (
    handle_post_debit_failure, handle_partial_failure,
)

_ctr = 0


def _n():
    global _ctr
    _ctr += 1
    return _ctr


def _make_reseller():
    n = _n()
    uid = 9_620_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P15A{n}", f"p15a{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P15A{n}", uid, f"p15a{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=()):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P15A Product {n}", price),
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P15A-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


def _today():
    from utils.timezone import now as _now
    return _now().strftime("%Y-%m-%d")


class Phase15AdjustmentTests(unittest.TestCase):
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

    # ------------------------------------------------------------ refunds

    def test_failure_refund_writes_record_atomically(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["FR-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        oid = self._order(key, sec, pub, 1, "p15a-fr1")
        handle_post_debit_failure(oid, reason="supplier_down")

        recs = adb.list_refunds(reseller_id=rs["id"])
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["kind"], "full")
        self.assertEqual(rec["source"], "system")
        self.assertEqual(rec["amount_cents"], 1000)
        self.assertEqual(rec["order_id"], oid)
        self.assertEqual(rec["order_public_id"], f"ORD-{oid:08d}")
        # record ↔ ledger chain
        db.cur.execute(
            "SELECT amount_cents FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND tx_type='refund_credit' AND reference=?",
            (rs["id"], rec["ledger_reference"]))
        self.assertEqual(int(db.cur.fetchone()[0]), 1000)
        # record immutable
        with self.assertRaises(Exception):
            db.cur.execute("UPDATE reseller_refunds SET amount_cents=1 WHERE id=?",
                           (rec["id"],))
            db.conn.commit()
        db.conn.rollback()

    def test_admin_refund_validation_and_caps(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["AV-1", "AV-2"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        oid = self._order(key, sec, pub, 2, "p15a-av1")   # $20.00 charged

        # refund $7 then $5 — multiple administrative refunds allowed
        r1 = issue_admin_refund(oid, 700, "goodwill tier 1", actor="owner")
        self.assertEqual(r1["amount_cents"], 700)
        # 50000 topup − 2000 charge + 700 refund = 48700
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 48700)
        r2 = issue_admin_refund(oid, 500, "goodwill tier 2", actor="owner")
        self.assertNotEqual(r1["public_id"], r2["public_id"])
        self.assertNotEqual(r1["ledger_reference"], r2["ledger_reference"])
        # +500 → 49200
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 49200)

        # refundable remainder is $8 — asking $9 must fail
        with self.assertRaises(AdjustmentError) as ctx:
            issue_admin_refund(oid, 900, "too much")
        self.assertEqual(ctx.exception.code, "refund_exceeds_refundable")
        # zero/negative rejected
        with self.assertRaises(AdjustmentError):
            issue_admin_refund(oid, 0, "zero")
        # unknown order
        with self.assertRaises(AdjustmentError):
            issue_admin_refund(99999999, 100, "ghost")
        # missing reason
        with self.assertRaises(AdjustmentError):
            issue_admin_refund(oid, 100, "  ")

        # full remaining refund works: 49200 + 800 = 50000 (fully restored)
        r3 = issue_admin_refund(oid, 800, "final settlement", actor="owner")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)
        with self.assertRaises(AdjustmentError) as ctx:
            issue_admin_refund(oid, 100, "nothing left")
        self.assertEqual(ctx.exception.code, "nothing_to_refund")

    def test_concurrent_admin_refunds_serialize(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["CR-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        oid = self._order(key, sec, pub, 1, "p15a-cr1")   # $10 refundable

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
        # $7 + $7 exceeds the $10 charge: exactly one succeeded
        self.assertEqual(len(results), 1, results)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "refund_exceeds_refundable")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 49700)
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_refunds WHERE order_id=?", (oid,))
        self.assertEqual(int(db.cur.fetchone()[0]), 1)

    def test_failure_refund_after_admin_refund_never_over_refunds(self):
        """The outstanding cap counts admin refunds: $10 order, $4 admin
        refund, then full failure refunds exactly $6."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["MX-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        oid = self._order(key, sec, pub, 1, "p15a-mx1")
        issue_admin_refund(oid, 400, "partial goodwill")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 49400)
        res = handle_post_debit_failure(oid, reason="later failure")
        self.assertTrue(res["refunded"])
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)
        # total refunds == charge
        db.cur.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND tx_type='refund_credit'", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 1000)

    # ------------------------------------------------------------ adjustments

    def test_adjustment_workflow_full_lifecycle(self):
        rs, key, sec = _make_reseller()
        _topup(rs["id"], 10000)

        adj = create_adjustment(rs["id"], "credit", 500, "promo credit",
                                created_by="owner")
        self.assertEqual(adj["status"], "pending")
        self.assertRegex(adj["public_id"], r"^ADJ-[A-HJ-NP-Z2-9]{10}$")
        # pending: no money
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 10000)

        approved = approve_adjustment(adj["id"], decided_by="owner")
        self.assertEqual(approved["status"], "approved")
        # approved: STILL no money (§16)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 10000)

        done = complete_adjustment(adj["id"], decided_by="owner")
        self.assertEqual(done["status"], "completed")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 10500)
        # record ↔ ledger chain
        db.cur.execute(
            "SELECT amount_cents FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND reference=?", (rs["id"], done["ledger_reference"]))
        self.assertEqual(int(db.cur.fetchone()[0]), 500)
        # double completion is a safe no-op
        again = complete_adjustment(adj["id"], decided_by="owner")
        self.assertTrue(again["is_duplicate"])
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 10500)

    def test_adjustment_reject_cancel_and_terminal_rules(self):
        rs, _k, _s = _make_reseller()
        _topup(rs["id"], 10000)

        a1 = create_adjustment(rs["id"], "debit", 300, "correction")
        r1 = reject_adjustment(a1["id"], decided_by="owner")
        self.assertEqual(r1["status"], "rejected")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 10000)
        with self.assertRaises(AdjustmentError):
            complete_adjustment(a1["id"])

        a2 = create_adjustment(rs["id"], "credit", 300, "mistake")
        c2 = cancel_adjustment(a2["id"], decided_by="owner")
        self.assertEqual(c2["status"], "cancelled")

        # terminal states are trigger-enforced
        with self.assertRaises(Exception):
            db.cur.execute("UPDATE reseller_adjustments SET status='pending' WHERE id=?",
                           (a1["id"],))
            db.conn.commit()
        db.conn.rollback()

        # financial fields frozen even while pending
        a3 = create_adjustment(rs["id"], "credit", 300, "x")
        with self.assertRaises(Exception):
            db.cur.execute("UPDATE reseller_adjustments SET amount_cents=999 WHERE id=?",
                           (a3["id"],))
            db.conn.commit()
        db.conn.rollback()

    def test_adjustment_validation_and_limits(self):
        rs, _k, _s = _make_reseller()
        with self.assertRaises(AdjustmentError):
            create_adjustment(rs["id"], "credit", 0, "zero")
        with self.assertRaises(AdjustmentError):
            create_adjustment(rs["id"], "credit", -5, "negative")
        with self.assertRaises(AdjustmentError):
            create_adjustment(rs["id"], "sideways", 100, "bad type")
        with self.assertRaises(AdjustmentError):
            create_adjustment(rs["id"], "credit", 100, "")
        with self.assertRaises(AdjustmentError):
            create_adjustment(99999999, "credit", 100, "ghost")
        # over the configurable ceiling
        huge = create_adjustment  # noqa
        with self.assertRaises(AdjustmentError) as ctx:
            create_adjustment(rs["id"], "credit", 100_000_001, "over limit")
        self.assertEqual(ctx.exception.code, "adjustment_limit_exceeded")

    def test_debit_adjustment_and_insufficient_funds(self):
        rs, _k, _s = _make_reseller()
        _topup(rs["id"], 1000)
        adj = create_adjustment(rs["id"], "debit", 400, "missed charge")
        approve_adjustment(adj["id"])
        done = complete_adjustment(adj["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 600)
        db.cur.execute(
            "SELECT amount_cents FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND tx_type='adjustment_debit'", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), -400)

        # debit larger than the balance fails at the ledger layer
        adj2 = create_adjustment(rs["id"], "debit", 999999, "over")
        approve_adjustment(adj2["id"])
        with self.assertRaises(AdjustmentError) as ctx:
            complete_adjustment(adj2["id"])
        self.assertEqual(ctx.exception.code, "insufficient_funds")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 600)
        # record is still 'approved', not completed
        self.assertEqual(adb.get_adjustment_by_id(adj2["id"])["status"], "approved")

    def test_webadmin_credit_creates_record(self):
        """The existing owner credit button now leaves a linked record."""
        from web_admin.wallet_admin_service import credit_as_admin
        rs, _k, _s = _make_reseller()
        result = credit_as_admin("owner", rs["id"], "25.00", "TP-REC-1", "topup")
        self.assertEqual(result.status, "posted")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 2500)
        # the record exists and links the ledger row
        adj = adb.list_adjustments(reseller_id=rs["id"])[0]
        self.assertEqual(adj["status"], "completed")
        self.assertEqual(adj["ledger_reference"], "TP-REC-1")
        self.assertEqual(adj["amount_cents"], 2500)
        self.assertEqual(adj["type"], "credit")
        # idempotent replay returns duplicate, never double-credits
        dup = credit_as_admin("owner", rs["id"], "25.00", "TP-REC-1", "topup")
        self.assertEqual(dup.status, "duplicate")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 2500)
        # non-owner blocked
        from web_admin.wallet_admin_service import AdminWalletError
        with self.assertRaises(AdminWalletError):
            credit_as_admin("admin", rs["id"], "1.00", "TP-REC-2", "x")

    def test_reversal_by_compensation(self):
        """An incorrect credit is corrected by a compensating debit — both
        remain in the ledger (history is never rewritten)."""
        rs, _k, _s = _make_reseller()
        _topup(rs["id"], 10000)
        a1 = create_adjustment(rs["id"], "credit", 500, "wrong credit")
        approve_adjustment(a1["id"])
        complete_adjustment(a1["id"])
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 10500)
        a2 = create_adjustment(rs["id"], "debit", 500,
                               "reversal of wrong credit")
        approve_adjustment(a2["id"])
        complete_adjustment(a2["id"])
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 10000)
        # both rows in history
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND tx_type LIKE 'adjustment_%'", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 2)
        # and both adjustments recorded
        self.assertEqual(len(adb.list_adjustments(reseller_id=rs["id"])), 2)

    # ------------------------------------------------------------ integration

    def test_invoice_statement_analytics_integration(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["IN-1", "IN-2"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15a-in1")
        today = _today()
        # finalize the invoice FIRST — then refund (invoice stays frozen)
        inv = generate_invoice(rs["id"], today, today)
        self.assertEqual(inv["total_cents"], 1000)

        issue_admin_refund(self._last_oid, 400, "post-invoice goodwill")
        # the finalized invoice is untouched
        inv2 = idb.get_invoice_by_id(inv["id"])
        self.assertEqual(inv2["total_cents"], 1000)
        # the refund appears on the NEXT document / in analytics
        wallet = rdb.get_wallet_financial_summary(rs["id"])
        self.assertEqual(wallet["refunds_cents"], 400)
        self.assertEqual(wallet["net_spend_cents"], 600)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 49400)

        # statement sees it
        from reseller_api.services.statement_service import generate_statement
        st = generate_statement(rs["id"], period_type="monthly",
                                month=_today()[:7])
        self.assertEqual(st["refunds_cents"], 400)
        self.assertEqual(st["closing_balance_cents"], 49400)

    _last_oid = 0

    def _order(self, key, sec, pub, qty, coid):
        r = self.client.post("/api/v1/orders", headers=_hdr(key, sec),
                             json={"product_id": pub, "quantity": qty,
                                   "client_order_id": coid})
        assert r.status_code == 201, r.get_json()
        type(self)._last_oid = int(r.get_json()["data"]["order_id"].replace("ORD-", ""))
        return type(self)._last_oid

    def test_reseller_refunds_api_isolation(self):
        rs, key, sec = _make_reseller()
        rs2, key2, sec2 = _make_reseller()
        pid, pub = _make_product(10.0, codes=["RI-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        oid = self._order(key, sec, pub, 1, "p15a-ri")
        rec = issue_admin_refund(oid, 600, "visible to owner only", actor="owner")

        H = _hdr(key, sec)
        r = self.client.get("/api/v1/refunds", headers=H)
        self.assertEqual(r.status_code, 200)
        rows = r.get_json()["data"]["refunds"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["refund_id"], rec["public_id"])
        self.assertEqual(rows[0]["amount"], "6.00")
        self.assertEqual(rows[0]["kind"], "administrative")
        self.assertEqual(rows[0]["reference"], rec["ledger_reference"])

        d = self.client.get(f"/api/v1/refunds/{rec['public_id']}", headers=H)
        self.assertEqual(d.status_code, 200)
        # foreign access -> 404; malformed -> 404; no auth -> 401
        self.assertEqual(self.client.get(f"/api/v1/refunds/{rec['public_id']}",
                                         headers=_hdr(key2, sec2)).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/refunds/REF-XXXXXXXXXX",
                                         headers=H).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/refunds/ORD-00000001",
                                         headers=H).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/refunds").status_code, 401)
        # resellers cannot create refunds or adjustments through the API
        self.assertIn(self.client.post("/api/v1/refunds", headers=H).status_code,
                      (404, 405))

    def test_reconciliation_covers_records(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["RR-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        oid = self._order(key, sec, pub, 1, "p15a-rr")
        issue_admin_refund(oid, 300, "check me")
        from reseller_api.services.reconciliation import reconcile_reseller
        self.assertEqual(reconcile_reseller(rs["id"]), [])

    def test_sandbox_marker_and_precision(self):
        from reseller_api.config import validate_environment_marker
        with self.assertRaises(RuntimeError):
            validate_environment_marker({"API_ENVIRONMENT": "production"})
        # decimal-safe: float amounts rejected end to end
        rs, _k, _s = _make_reseller()
        with self.assertRaises(Exception):
            rdb.post_wallet_transaction(rs["id"], "adjustment_credit",
                                        10.5, "P15A-FLT", "x")


if __name__ == "__main__":
    unittest.main()
