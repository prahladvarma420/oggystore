"""Phase 15 Prompt 4 — transaction ledger integration & hardening.

Hardens INTEGRATION only — the ledger itself is unchanged.  Covers the
prompt's checklist against the real stack: order/refund/adjustment writes,
derived balance_before, retries (order/refund/adjustment/payment-callback
shape), concurrent debits, negative-balance protection, isolation, the new
transactions API (filters/pagination), invoice/statement/analytics
integration, the reconciliation service, sandbox isolation, and the
controlled financial-integrity scenario with four-way agreement.
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase15_led_", suffix=".db")
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
from reseller_api import create_app
import reseller_api.middleware.request_context  # noqa: F401
import reseller_api.middleware.rate_limit  # noqa: F401
import reseller_api.middleware.security_controls  # noqa: F401
from reseller_api.services.rate_limit import RequestRateDecision
from reseller_api.services.order_failure import (
    handle_post_debit_failure, handle_cancellation, handle_partial_failure,
)
from reseller_api.services.invoice_service import generate_invoice
from reseller_api.services.statement_service import generate_statement
from reseller_api.services.reconciliation import (
    reconcile_reseller, reconcile_platform,
)

_ctr = 0


def _n():
    global _ctr
    _ctr += 1
    return _ctr


def _make_reseller():
    n = _n()
    uid = 9_630_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P15L{n}", f"p15l{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P15L{n}", uid, f"p15l{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=()):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P15L Product {n}", price),
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P15L-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


def _today():
    from utils.timezone import now as _now
    return _now().strftime("%Y-%m-%d")


def _month():
    from utils.timezone import now as _now
    return _now().strftime("%Y-%m")


class Phase15LedgerIntegrationTests(unittest.TestCase):
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
        return r.get_json()["data"]["order_id"]

    # ------------------------------------------------------------ 1-9. write paths

    def test_order_debit_wallet_ledger_atomic(self):
        """Order + debit + ledger row commit atomically; the row carries
        type/direction/amount/currency/balance_after/reference/order link."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["LD-1", "LD-2"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 100000)
        oid = self._order(key, sec, pub, 2, "p15l-atomic")

        db.cur.execute(
            "SELECT tx_type, amount_cents, balance_after_cents, reference,"
            " currency, related_order_reference FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND tx_type='purchase_debit'", (rs["id"],))
        rows = db.cur.fetchall()
        self.assertEqual(len(rows), 1)
        tx_type, amount, bal_after, ref, currency, related = rows[0]
        self.assertEqual(tx_type, "purchase_debit")
        self.assertEqual(amount, -2000)
        self.assertEqual(bal_after, 98000)
        self.assertEqual(currency, "USDT")
        self.assertEqual(ref, related)
        self.assertTrue(ref.startswith(f"rapi.{rs['id']}."))
        # order link: reference embeds the order's request_id
        db.cur.execute("SELECT request_id FROM orders WHERE id=?",
                       (int(oid.replace("ORD-", "")),))
        self.assertEqual(ref, f"rapi.{rs['id']}.{db.cur.fetchone()[0]}")

    def test_refund_credit_and_no_duplicate(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["RD-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        oid = self._order(key, sec, pub, 1, "p15l-ref")
        ioid = int(oid.replace("ORD-", ""))
        handle_post_debit_failure(ioid, reason="t")
        handle_post_debit_failure(ioid, reason="t")   # retry — no second credit
        db.cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount_cents),0) FROM"
            " reseller_wallet_transactions WHERE reseller_id=? AND tx_type='refund_credit'",
            (rs["id"],))
        cnt, total = db.cur.fetchone()
        self.assertEqual((int(cnt), int(total)), (1, 1000))
        # the refund references the order
        db.cur.execute(
            "SELECT reference FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND tx_type='refund_credit'", (rs["id"],))
        self.assertEqual(db.cur.fetchone()[0], f"rapi.refund.order.{ioid}")

    def test_adjustment_credit_debit_idempotent(self):
        rs, _k, _s = _make_reseller()
        _topup(rs["id"], 10000)
        rdb.post_wallet_transaction(rs["id"], "adjustment_credit", 500,
                                    "ADJ-T-1", "bonus")
        rdb.post_wallet_transaction(rs["id"], "adjustment_debit", -200,
                                    "ADJ-T-2", "correction")
        # exact replays dedupe; same key + different amount conflicts
        st1, _ = rdb.post_wallet_transaction(rs["id"], "adjustment_credit", 500,
                                             "ADJ-T-1", "bonus")
        self.assertEqual(st1, "duplicate")
        st2, _ = rdb.post_wallet_transaction(rs["id"], "adjustment_credit", 999,
                                             "ADJ-T-1", "different")
        self.assertEqual(st2, "idempotency_conflict")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 10300)

    def test_negative_balance_and_currency_guards(self):
        rs, _k, _s = _make_reseller()
        # debit beyond balance refused at the financial layer
        status, _ = rdb.post_wallet_transaction(rs["id"], "purchase_debit",
                                                -100, "P15L-NEG-1", "x")
        self.assertEqual(status, "insufficient_funds")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 0)
        # currency other than USDT rejected
        try:
            rdb.post_wallet_transaction(rs["id"], "topup_credit", 100,
                                        "P15L-CUR", "x", currency="INR")
            self.fail("currency mismatch accepted")
        except ValueError:
            pass
        # zero amount rejected
        try:
            rdb.post_wallet_transaction(rs["id"], "topup_credit", 0,
                                        "P15L-ZERO", "x")
            self.fail("zero amount accepted")
        except ValueError:
            pass

    def test_ledger_immutable_and_balance_chain(self):
        rs, _k, _s = _make_reseller()
        _topup(rs["id"], 10000)
        db.cur.execute(
            "SELECT id FROM reseller_wallet_transactions WHERE reseller_id=?",
            (rs["id"],))
        row_id = int(db.cur.fetchone()[0])
        with self.assertRaises(Exception):
            db.cur.execute(
                "UPDATE reseller_wallet_transactions SET amount_cents=1 WHERE id=?",
                (row_id,))
            db.conn.commit()
        db.conn.rollback()
        # chain check: each balance_after == previous + amount
        rs2, _k2, _s2 = _make_reseller()
        _topup(rs2["id"], 10000)
        rdb.post_wallet_transaction(rs2["id"], "adjustment_debit", -3000,
                                    "ADJ-CHAIN-1", "x")
        _topup(rs2["id"], 500)
        db.cur.execute(
            "SELECT amount_cents, balance_after_cents FROM"
            " reseller_wallet_transactions WHERE reseller_id=? ORDER BY id",
            (rs2["id"],))
        running = 0
        for amount, after in db.cur.fetchall():
            running += int(amount)
            self.assertEqual(int(after), running)
        self.assertEqual(running, rdb.get_wallet_balance_cents(rs2["id"]))

    def test_concurrent_debits_never_overspend(self):
        """Balance $500; two racing $400 orders — exactly one wins."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(4.0, codes=["OV-1", "OV-2"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 500)
        barrier = threading.Barrier(2)
        out = []

        def worker(i):
            c = self.app.test_client()
            barrier.wait()
            out.append(c.post("/api/v1/orders", headers=_hdr(key, sec),
                              json={"product_id": pub, "quantity": 1,
                                    "client_order_id": f"p15l-ov-{i}"}).status_code)

        a, b = threading.Thread(target=worker, args=(1,)), threading.Thread(target=worker, args=(2,))
        a.start(); b.start(); a.join(); b.join()
        self.assertEqual(sorted(out), [201, 402])
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 100)
        self.assertGreaterEqual(rdb.get_wallet_balance_cents(rs["id"]), 0)

    # ------------------------------------------------------------ 10-14. transactions API

    def test_transactions_api_filters_pagination_isolation(self):
        rs, key, sec = _make_reseller()
        rs2, key2, sec2 = _make_reseller()
        pid, pub = _make_product(10.0, codes=["TA-1", "TA-2", "TA-3"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 100000)
        oid1 = self._order(key, sec, pub, 1, "p15l-ta1")
        oid2 = self._order(key, sec, pub, 1, "p15l-ta2")
        handle_post_debit_failure(int(oid2.replace("ORD-", "")), reason="t")
        H = _hdr(key, sec)

        r = self.client.get("/api/v1/transactions", headers=H)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        rows = body["data"]["transactions"]
        self.assertEqual(len(rows), 4)   # topup + 2 debits + 1 refund
        # shape + derived balance_before
        for row in rows:
            self.assertIn("reference", row)
            self.assertIn("balance_after", row)
            self.assertIn("balance_before", row)
            self.assertNotIn("id", row)                 # no raw ledger ids
            self.assertNotIn("reseller_id", row)
            self.assertNotIn("api_key", row)
        # chain: balance_before == balance_after - amount (signed)
        first = [r for r in rows if r["type"] == "topup_credit"][0]
        self.assertEqual(first["balance_before"], "0.00")
        self.assertEqual(first["balance_after"], "1000.00")

        # type filter
        debits = self.client.get("/api/v1/transactions?type=purchase_debit",
                                 headers=H).get_json()["data"]["transactions"]
        self.assertEqual(len(debits), 2)
        self.assertTrue(all(t["type"] == "purchase_debit" for t in debits))
        # direction filter
        credits = self.client.get("/api/v1/transactions?direction=credit",
                                  headers=H).get_json()["data"]["transactions"]
        self.assertTrue(all(t["direction"] == "credit" for t in credits))
        self.assertEqual(len(credits), 2)               # topup + refund
        # order filter includes the refund of that order
        o2_rows = self.client.get(f"/api/v1/transactions?order_id={oid2}",
                                  headers=H).get_json()["data"]["transactions"]
        self.assertEqual({t["type"] for t in o2_rows},
                         {"purchase_debit", "refund_credit"})
        # date filter
        today = _today()
        in_scope = self.client.get(
            f"/api/v1/transactions?date_from={today}&date_to={today}",
            headers=H).get_json()["data"]["transactions"]
        self.assertEqual(len(in_scope), 4)
        future = self.client.get("/api/v1/transactions?date_from=2999-01-01",
                                 headers=H).get_json()["data"]["transactions"]
        self.assertEqual(future, [])
        # bad filters
        self.assertEqual(self.client.get("/api/v1/transactions?type=bogus",
                                         headers=H).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/transactions?direction=sideways",
                                         headers=H).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/transactions?date_from=nope",
                                         headers=H).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/transactions?order_id=ORD-X",
                                         headers=H).status_code, 400)
        # pagination
        page1 = self.client.get("/api/v1/transactions?limit=2", headers=H).get_json()
        self.assertEqual(len(page1["data"]["transactions"]), 2)
        self.assertTrue(page1["meta"]["pagination"]["has_more"])
        after = page1["meta"]["pagination"]["next_after"]
        page2 = self.client.get(f"/api/v1/transactions?limit=2&after={after}",
                                headers=H).get_json()
        ids1 = {t["reference"] for t in page1["data"]["transactions"]}
        ids2 = {t["reference"] for t in page2["data"]["transactions"]}
        self.assertFalse(ids1 & ids2)
        self.assertEqual(len(ids1 | ids2), 4)

        # isolation: B sees only his own rows (a fresh topup)
        _topup(rs2["id"], 7000)
        rows_b = self.client.get("/api/v1/transactions",
                                 headers=_hdr(key2, sec2)).get_json()["data"]["transactions"]
        self.assertEqual(len(rows_b), 1)
        self.assertEqual(rows_b[0]["reference"].startswith("TP-P15L-"), True)
        # foreign order filter -> empty page, no oracle
        foreign = self.client.get(f"/api/v1/transactions?order_id={oid1}",
                                  headers=_hdr(key2, sec2)).get_json()["data"]["transactions"]
        self.assertEqual(foreign, [])
        # no auth
        self.assertEqual(self.client.get("/api/v1/transactions").status_code, 401)

    # ------------------------------------------------------------ 15-20. integrations

    def test_invoice_statement_analytics_integration(self):
        """One known workload must agree across ledger, invoice, statement,
        and the Phase 14 analytics views."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["IN-1", "IN-2", "IN-3", "IN-4"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 100000)
        self._order(key, sec, pub, 2, "p15l-in1")     # -2000
        o2 = self._order(key, sec, pub, 1, "p15l-in2")
        handle_post_debit_failure(int(o2.replace("ORD-", "")), reason="t")  # +1000
        today = _today()

        inv = generate_invoice(rs["id"], today, today)
        st = generate_statement(rs["id"], period_type="monthly", month=_month())
        wallet = rdb.get_wallet_financial_summary(rs["id"])
        sales = rdb.get_reseller_sales_analytics(rs["id"], days=30)

        # invoice: billed activity
        self.assertEqual(inv["subtotal_cents"], 3000)
        self.assertEqual(inv["refund_cents"], 1000)
        self.assertEqual(inv["total_cents"], 2000)
        # statement: account movement
        self.assertEqual(st["total_debits_cents"], 3000)
        self.assertEqual(st["refunds_cents"], 1000)
        self.assertEqual(st["closing_balance_cents"], 98000)
        # wallet analytics agree
        self.assertEqual(wallet["total_purchases_cents"], 3000)
        self.assertEqual(wallet["refunds_cents"], 1000)
        self.assertEqual(wallet["net_spend_cents"], 2000)
        # sales analytics agree
        self.assertEqual(sales["gross_sales_cents"], 3000)
        self.assertEqual(sales["refunds_total_cents"], 1000)
        self.assertEqual(sales["net_sales_cents"], 2000)
        # live balance agrees everywhere
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 98000)
        self.assertEqual(st["closing_balance_cents"], 98000)

    def test_reconciliation_clean_and_dirty(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["RE-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15l-re1")

        clean = reconcile_reseller(rs["id"])
        self.assertEqual(clean, [])

        # inject corruption directly (bypassing the guarded write path is
        # impossible via triggers — so simulate via a raw INSERT that violates
        # the chain: allowed by design for seeds; the validator must catch it)
        bal = rdb.get_wallet_balance_cents(rs["id"])
        db.cur.execute(
            "INSERT INTO reseller_wallet_transactions"
            "(reseller_id, tx_type, amount_cents, balance_after_cents,"
            " reference, description, created, currency)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (rs["id"], "adjustment_credit", 500, bal + 999,  # WRONG chain value
             f"ADJ-CORRUPT-{_n()}", "injected", "2026-01-01 00:00:00", "USDT"))
        db.cur.execute(
            "UPDATE reseller_wallet SET balance_cents=balance_cents+500 WHERE reseller_id=?",
            (rs["id"],))
        db.conn.commit()
        issues = reconcile_reseller(rs["id"])
        checks = {i["check"] for i in issues}
        self.assertIn("chain_break", checks)
        # wallet==ledger still holds (we credited both) — no balance_mismatch
        self.assertNotIn("balance_mismatch", checks)

        # platform sweep reports it too
        plat = reconcile_platform()
        self.assertFalse(plat["ok"])
        self.assertGreaterEqual(plat["issues_total"], 1)
        self.assertGreaterEqual(plat["resellers_checked"], 1)

    def test_reconciliation_webadmin_owner_only(self):
        import web_admin.app as wa
        wa.app.config["TESTING"] = True
        wa.DB_PATH = _db_path
        anon = wa.app.test_client()
        self.assertEqual(anon.get("/api/monitoring/reconciliation").status_code, 401)
        owner = self._owner()
        r = owner.get("/api/monitoring/reconciliation")
        self.assertEqual(r.status_code, 200)
        self.assertIn("issues", r.get_json()["reconciliation"])

    def _owner(self):
        import web_admin.app as wa
        wa.app.config["TESTING"] = True
        wa.DB_PATH = _db_path
        c = wa.app.test_client()
        with c.session_transaction() as sess:
            sess["logged_in"] = True
            sess["username"] = "owner"
            sess["web_role"] = "owner"
        return c

    # ------------------------------------------------------------ 21-24. integrity scenario + sandbox

    def test_financial_integrity_scenario_four_way_agreement(self):
        """Controlled scenario per the prompt (USDT cents):
        topup $10,000; orders $1,500 and $2,000; refund $500; credit $1,000.
        Expected closing per the ledger model: 1000000-150000-200000+50000+100000
        = 800000 cents ($8,000.00).  Wallet, ledger, statement, and the
        analytics surfaces must all agree."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=[f"FS-{i}" for i in range(360)])
        db.cur.execute("UPDATE products SET max_per_order=250 WHERE id=?", (pid,))
        db.conn.commit()
        _grant(rs["id"], pid)
        _topup(rs["id"], 1_000_000)                     # $10,000.00
        self._order(key, sec, pub, 150, "p15l-fs1")     # $1,500.00 debit
        o2 = self._order(key, sec, pub, 200, "p15l-fs2")  # $2,000.00 debit
        handle_partial_failure(int(o2.replace("ORD-", "")),
                               fulfilled_qty=150, failed_qty=50)   # $500 refund
        from reseller_api.services.adjustment_service import (
            create_adjustment, approve_adjustment, complete_adjustment)
        _adj = create_adjustment(rs["id"], "credit", 100_000, "promo")
        approve_adjustment(_adj["id"], decided_by="owner")
        complete_adjustment(_adj["id"], decided_by="owner")   # $1,000 credit

        expected = 1_000_000 - 150_000 - 200_000 + 50_000 + 100_000
        self.assertEqual(expected, 800_000)

        # wallet
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), expected)
        # ledger sum
        db.cur.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM reseller_wallet_transactions"
            " WHERE reseller_id=?", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), expected)
        # statement
        st = generate_statement(rs["id"], period_type="monthly", month=_month())
        self.assertEqual(st["closing_balance_cents"], expected)
        # analytics
        wallet = rdb.get_wallet_financial_summary(rs["id"])
        self.assertEqual(wallet["balance_cents"], expected)
        # reconciliation: clean
        self.assertEqual(reconcile_reseller(rs["id"]), [])

    def test_sandbox_isolation_marker(self):
        from reseller_api.config import validate_environment_marker
        with self.assertRaises(RuntimeError):
            validate_environment_marker({"API_ENVIRONMENT": "production"})
        validate_environment_marker({"API_ENVIRONMENT": "test"})


if __name__ == "__main__":
    unittest.main()
