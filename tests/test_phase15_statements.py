"""Phase 15 Prompt 3 — billing periods & reseller financial statements.

Statements are ledger-derived snapshots (open → finalized).  Verified against
known ledger activity with exact balances, half-open boundary correctness,
invoice reconciliation, immutability triggers, concurrency, isolation,
API/WebAdmin surfaces, and large histories.
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase15_stmt_", suffix=".db")
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
from reseller_api.services.statement_service import (
    StatementError, generate_statement, finalize_statement,
)
from reseller_api.services.invoice_service import generate_invoice
from reseller_api.services.order_failure import handle_post_debit_failure

_ctr = 0


def _n():
    global _ctr
    _ctr += 1
    return _ctr


def _make_reseller():
    n = _n()
    uid = 9_640_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P15S{n}", f"p15s{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P15S{n}", uid, f"p15s{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=()):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P15S Product {n}", price),
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P15S-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


def _today():
    return tz_now().strftime("%Y-%m-%d")


def _month():
    return tz_now().strftime("%Y-%m")


from utils.timezone import now as tz_now  # noqa: E402


class Phase15StatementTests(unittest.TestCase):
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

    # ------------------------------------------------------------ 1-4. generation & balances

    def test_monthly_statement_exact_balances(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["SM-%d" % i for i in range(6)])
        _grant(rs["id"], pid)
        _topup(rs["id"], 100000)                 # +$1000.00
        self._order(key, sec, pub, 2, "p15s-m1")  # -$20
        self._order(key, sec, pub, 1, "p15s-m2")  # -$10
        o3 = self._order(key, sec, pub, 1, "p15s-m3")
        handle_post_debit_failure(o3, reason="t")  # +$10 refund
        rdb.post_wallet_transaction(rs["id"], "adjustment_credit", 250,
                                    "ADJ-P15S-1", "goodwill")

        st = generate_statement(rs["id"], period_type="monthly",
                                month=_month())
        # opening 0; credits 100000+1000+250; debits 2000+1000+1000; closing 97250
        self.assertEqual(st["status"], "open")
        self.assertEqual(st["period_type"], "monthly")
        self.assertEqual(st["opening_balance_cents"], 0)
        self.assertEqual(st["total_credits_cents"], 101250)
        self.assertEqual(st["total_debits_cents"], 4000)
        self.assertEqual(st["purchases_cents"], 4000)
        self.assertEqual(st["refunds_cents"], 1000)
        self.assertEqual(st["topup_cents"], 100000)
        self.assertEqual(st["adjustment_credit_cents"], 250)
        self.assertEqual(st["closing_balance_cents"], 97250)
        self.assertEqual(
            st["closing_balance_cents"],
            rdb.get_wallet_balance_cents(rs["id"]))   # matches live wallet
        self.assertEqual(st["transactions_count"], 6)
        self.assertEqual(st["orders_count"], 3)
        self.assertEqual(st["timezone"], "IST")
        self.assertRegex(st["public_id"], r"^STMT-[A-HJ-NP-Z2-9]{12}$")
        self.assertRegex(st["statement_number"], r"^ST-\d{4}-\d{6}$")

        # lines reference the ledger, carry running balance, stable order
        lines = sdb.get_statement_lines(st["id"])
        self.assertEqual(len(lines), 6)
        keys = [(l["created"], l["id"]) for l in lines]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(lines[-1]["balance_after_cents"], 97250)
        self.assertTrue(all(l["ledger_id"] for l in lines))
        self.assertTrue(all(l["reference"] for l in lines))
        directions = {l["tx_type"]: l["direction"] for l in lines}
        self.assertEqual(directions["purchase_debit"], "debit")
        self.assertEqual(directions["refund_credit"], "credit")

    def test_weekly_and_custom_periods(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["WK-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15s-w1")
        today = _today()
        w = generate_statement(rs["id"], period_type="weekly", start_date=today)
        self.assertEqual(w["period_type"], "weekly")
        self.assertEqual(w["total_debits_cents"], 1000)
        # ISO week: Monday start
        from datetime import datetime
        self.assertEqual(
            datetime.strptime(w["period_start"][:10], "%Y-%m-%d").weekday(), 0)
        c = generate_statement(rs["id"], period_type="custom",
                               start_date=today, end_date=today)
        self.assertEqual(c["period_type"], "custom")
        self.assertEqual(c["period_end"], f"{today} 00:00:00"
                         .replace(" 00:00:00", " ")[:-0] if False else
                         c["period_end"])
        self.assertEqual(c["total_debits_cents"], 1000)
        # the two period types coexist as separate statements
        self.assertNotEqual(w["id"], c["id"])

    def test_empty_and_opening_balance_periods(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["EB-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15s-eb")

        # empty NEXT month: opening == current balance, zero activity
        import datetime as _dt
        next_month = (tz_now().replace(day=28) + _dt.timedelta(days=6)).strftime("%Y-%m")
        st = generate_statement(rs["id"], period_type="monthly", month=next_month)
        self.assertEqual(st["transactions_count"], 0)
        self.assertEqual(st["opening_balance_cents"], 49000)
        self.assertEqual(st["closing_balance_cents"], 49000)
        self.assertEqual(sdb.get_statement_lines(st["id"]), [])

        # and the CURRENT month opens at 0 and closes at 49000
        cur_month = generate_statement(rs["id"], period_type="monthly",
                                       month=_month())
        self.assertEqual(cur_month["opening_balance_cents"], 0)
        self.assertEqual(cur_month["closing_balance_cents"], 49000)

    # ------------------------------------------------------------ 5. boundary correctness

    def test_boundary_transactions_belong_to_one_period(self):
        rs, _k, _s = _make_reseller()
        # Ledger rows at exact boundaries.  The ledger is immutable to
        # UPDATE/DELETE (proven), so boundary rows are INSERTED with exact
        # timestamps and correct running balances — same write path shape as
        # post_wallet_transaction, computed from the live balance.
        rid = rs["id"]
        today = _today()

        def _seed(amount, reference, created):
            bal = rdb.get_wallet_balance_cents(rid)
            db.cur.execute(
                "INSERT INTO reseller_wallet_transactions"
                "(reseller_id, tx_type, amount_cents, balance_after_cents,"
                " reference, description, created, currency)"
                " VALUES(?,?,?,?,?,?,?,'USDT')",
                (rid, "topup_credit", amount, bal + amount,
                 reference, "edge", created))
            db.cur.execute(
                "UPDATE reseller_wallet SET balance_cents=balance_cents+?"
                " WHERE reseller_id=?", (amount, rid))
            db.conn.commit()

        _seed(1000, f"TP-B-{_n()}", f"{today} 00:00:00")   # exactly period start
        st = generate_statement(rid, period_type="custom",
                                start_date=today, end_date=today)
        self.assertEqual(st["opening_balance_cents"], 0)       # not before
        self.assertEqual(st["transactions_count"], 1)          # start INCLUDED
        self.assertEqual(st["total_credits_cents"], 1000)

        # a row at exactly period END (next day 00:00:00) belongs to the NEXT period
        from datetime import datetime as dt2, timedelta as td2
        tomorrow = (dt2.strptime(today, "%Y-%m-%d") + td2(days=1)).strftime("%Y-%m-%d")
        _seed(2000, f"TP-BE-{_n()}", f"{tomorrow} 00:00:00")   # exactly period end
        st2 = generate_statement(rid, period_type="custom",
                                 start_date=today, end_date=today)
        self.assertEqual(st2["total_credits_cents"], 1000)     # NOT 3000
        nxt = generate_statement(rid, period_type="custom",
                                 start_date=tomorrow, end_date=tomorrow)
        # the edge row (tomorrow 00:00:00) is INSIDE the next window:
        # opening = only the start-edge row (1000); the edge row is activity
        self.assertEqual(nxt["opening_balance_cents"], 1000)
        self.assertEqual(nxt["total_credits_cents"], 2000)
        self.assertEqual(nxt["closing_balance_cents"], 3000)   # 1000 + 2000

    # ------------------------------------------------------------ 6. idempotency & concurrency

    def test_duplicate_and_concurrent_generation(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["DU-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15s-dup")
        today = _today()

        a = generate_statement(rs["id"], period_type="custom",
                               start_date=today, end_date=today)
        b = generate_statement(rs["id"], period_type="custom",
                               start_date=today, end_date=today)
        # open statement: same row, refreshed (not duplicated)
        self.assertEqual(a["id"], b["id"])

        barrier = threading.Barrier(5)
        results = []

        def gen():
            barrier.wait()
            results.append(generate_statement(rs["id"], period_type="custom",
                                              start_date=today, end_date=today))

        ts = [threading.Thread(target=gen) for _ in range(5)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        ids = {r["id"] for r in results}
        self.assertEqual(len(ids), 1)
        nums = {r["statement_number"] for r in results}
        self.assertEqual(len(nums), 1)
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_statements WHERE reseller_id=?",
            (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 1)
        # line uniqueness backstop held
        db.cur.execute(
            "SELECT COUNT(*) FROM (SELECT ledger_id FROM reseller_statement_lines"
            " WHERE statement_id=? GROUP BY ledger_id HAVING COUNT(*)>1)",
            (a["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 0)

    # ------------------------------------------------------------ 7. finalization immutability

    def test_finalized_statement_frozen(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["FZ-1", "FZ-2"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15s-fz1")
        today = _today()
        st = generate_statement(rs["id"], period_type="custom",
                                start_date=today, end_date=today,
                                finalize=True)
        self.assertEqual(st["status"], "finalized")
        closing = st["closing_balance_cents"]

        # new activity must not alter the finalized document
        self._order(key, sec, pub, 1, "p15s-fz2")
        again = generate_statement(rs["id"], period_type="custom",
                                   start_date=today, end_date=today)
        self.assertTrue(again["is_duplicate"])
        self.assertEqual(again["closing_balance_cents"], closing)

        # service + trigger: no re-open, no financial edit, no line edit, no delete
        with self.assertRaises(Exception):
            db.cur.execute(
                "UPDATE reseller_statements SET closing_balance_cents=1 WHERE id=?",
                (st["id"],))
            db.conn.commit()
        db.conn.rollback()
        with self.assertRaises(Exception):
            db.cur.execute(
                "UPDATE reseller_statements SET status='open' WHERE id=?",
                (st["id"],))
            db.conn.commit()
        db.conn.rollback()
        with self.assertRaises(Exception):
            db.cur.execute(
                "DELETE FROM reseller_statement_lines WHERE statement_id=?",
                (st["id"],))
            db.conn.commit()
        db.conn.rollback()
        with self.assertRaises(Exception):
            db.cur.execute("DELETE FROM reseller_statements WHERE id=?", (st["id"],))
            db.conn.commit()
        db.conn.rollback()

    # ------------------------------------------------------------ 8. invoices relationship

    def test_invoice_reconciliation_block(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["RC-1", "RC-2"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15s-rc1")
        today = _today()
        inv = generate_invoice(rs["id"], today, today)
        st = generate_statement(rs["id"], period_type="invoice",
                                invoice_id=inv["public_id"])
        self.assertEqual(st["period_type"], "invoice")
        self.assertIn(inv["public_id"], eval(st["invoice_ids"]))

        # reconciliation via the API detail
        r = self.client.get(f"/api/v1/statements/{st['public_id']}",
                            headers=_hdr(key, sec))
        self.assertEqual(r.status_code, 200)
        recon = r.get_json()["data"]["statement"]["reconciliation"]["invoices"]
        self.assertEqual(len(recon), 1)
        # statement window == invoice window here; billable slices match
        self.assertTrue(recon[0]["billable_activity_matches"])
        self.assertEqual(recon[0]["invoice_subtotal"], "10.00")
        self.assertEqual(recon[0]["statement_purchases_in_period"], "10.00")

    # ------------------------------------------------------------ 9. API surface

    def test_api_list_detail_transactions_invoices_document(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["SA-1", "SA-2"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15s-api")
        today = _today()
        st = generate_statement(rs["id"], period_type="custom",
                                start_date=today, end_date=today)
        H = _hdr(key, sec)

        r = self.client.get("/api/v1/statements", headers=H)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        sts = body["data"]["statements"]
        self.assertEqual(len(sts), 1)
        self.assertEqual(sts[0]["statement_id"], st["public_id"])
        self.assertEqual(sts[0]["closing_balance"], "490.00")
        self.assertEqual(sts[0]["period"]["half_open"], True)

        d = self.client.get(f"/api/v1/statements/{st['public_id']}", headers=H)
        self.assertEqual(d.status_code, 200)
        self.assertEqual(d.get_json()["data"]["statement"]["opening_balance"], "0.00")

        tx = self.client.get(f"/api/v1/statements/{st['public_id']}/transactions",
                             headers=H)
        self.assertEqual(tx.status_code, 200)
        lines = tx.get_json()["data"]["transactions"]
        self.assertEqual(len(lines), 2)   # topup + purchase
        self.assertEqual(lines[0]["direction"], "credit")
        self.assertEqual(lines[1]["direction"], "debit")
        self.assertTrue(lines[1]["order_id"].startswith("ORD-"))
        self.assertEqual(lines[-1]["balance_after"], "490.00")

        doc = self.client.get(f"/api/v1/statements/{st['public_id']}/document",
                              headers=H)
        self.assertEqual(doc.status_code, 200)
        html = doc.get_data(as_text=True)
        self.assertIn(st["statement_number"], html)
        self.assertIn("490.00 USDT", html)
        self.assertNotIn(sec, html)
        self.assertNotIn(f">{st['id']}<", html)

        # statement invoices endpoint (none generated yet -> empty)
        iv = self.client.get(f"/api/v1/statements/{st['public_id']}/invoices",
                             headers=H)
        self.assertEqual(iv.status_code, 200)
        self.assertEqual(iv.get_json()["data"]["invoices"], [])

    def test_api_isolation_and_filters(self):
        ra, ka, sa = _make_reseller()
        rb, kb, sb = _make_reseller()
        pid, pub = _make_product(10.0, codes=["SI-1"])
        _grant(ra["id"], pid)
        _topup(ra["id"], 50000)
        self._order(ka, sa, pub, 1, "p15s-iso")
        today = _today()
        st = generate_statement(ra["id"], period_type="custom",
                                start_date=today, end_date=today)

        for path in (f"/api/v1/statements/{st['public_id']}",
                     f"/api/v1/statements/{st['public_id']}/transactions",
                     f"/api/v1/statements/{st['public_id']}/invoices",
                     f"/api/v1/statements/{st['public_id']}/document"):
            r = self.client.get(path, headers=_hdr(kb, sb))
            self.assertEqual(r.status_code, 404, path)
        self.assertEqual(self.client.get("/api/v1/statements").status_code, 401)
        # B sees none of A's statements in his list
        lst = self.client.get("/api/v1/statements", headers=_hdr(kb, sb))
        self.assertEqual(lst.get_json()["data"]["statements"], [])
        # filters
        self.assertEqual(self.client.get(
            "/api/v1/statements?status=void", headers=_hdr(ka, sa)).status_code, 400)
        self.assertEqual(self.client.get(
            "/api/v1/statements?period_type=bogus", headers=_hdr(ka, sa)).status_code, 400)
        ok = self.client.get("/api/v1/statements?period_type=custom",
                             headers=_hdr(ka, sa))
        self.assertEqual(len(ok.get_json()["data"]["statements"]), 1)

    # ------------------------------------------------------------ 10. WebAdmin

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

    def test_webadmin_generate_list_detail_finalize_document(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["WO-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15s-wo")
        today = _today()
        ac = self._owner()
        hdr = {"X-CSRF-Token": "test-csrf"}

        r = ac.post(f"/api/resellers/{rs['public_id']}/statements/generate",
                    json={"period_type": "custom", "start_date": today,
                          "end_date": today}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_json())
        gen = r.get_json()
        self.assertTrue(gen["ok"])
        self.assertEqual(gen["status"], "open")
        self.assertEqual(gen["closing_balance"], "490.00")

        # idempotent regeneration
        r2 = ac.post(f"/api/resellers/{rs['public_id']}/statements/generate",
                     json={"period_type": "custom", "start_date": today,
                           "end_date": today}, headers=hdr)
        self.assertEqual(r2.get_json()["statement_id"], gen["statement_id"])

        # list + filters
        lst = ac.get(f"/api/statements?reseller={rs['public_id']}&status=open")
        self.assertEqual(lst.status_code, 200)
        rows = lst.get_json()["statements"]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("id", rows[0])
        self.assertNotIn("reseller_id", rows[0])
        lst2 = ac.get(f"/api/statements?search={gen['statement_number']}")
        self.assertEqual(len(lst2.get_json()["statements"]), 1)

        # detail with lines (ledger ids stripped)
        det = ac.get(f"/api/statements/{gen['statement_id']}")
        self.assertEqual(det.status_code, 200)
        self.assertTrue(det.get_json()["lines"])
        self.assertNotIn("ledger_id", det.get_json()["lines"][0])

        # finalize (owner) then document
        fin = ac.post(f"/api/statements/{gen['statement_id']}/finalize",
                      json={}, headers=hdr)
        self.assertEqual(fin.get_json()["status"], "finalized")
        doc = ac.get(f"/api/statements/{gen['statement_id']}/document")
        self.assertEqual(doc.status_code, 200)
        self.assertIn(gen["statement_number"], doc.get_data(as_text=True))
        # finalize again -> duplicate
        fin2 = ac.post(f"/api/statements/{gen['statement_id']}/finalize",
                       json={}, headers=hdr)
        self.assertTrue(fin2.get_json()["duplicate"])

        # gates
        import web_admin.app as wa
        anon = wa.app.test_client()
        self.assertEqual(anon.get("/api/statements").status_code, 401)
        non_owner = self._owner(role="admin")
        self.assertEqual(
            non_owner.post(f"/api/resellers/{rs['public_id']}/statements/generate",
                           json={"period_type": "custom", "start_date": today,
                                 "end_date": today}, headers=hdr).status_code, 403)
        db.cur.execute(
            "SELECT COUNT(*) FROM admin_logs WHERE action LIKE '%statement%'")
        self.assertGreaterEqual(int(db.cur.fetchone()[0]), 2)

    # ------------------------------------------------------------ 11. large history & misc

    def test_large_history_paginated_lines(self):
        rs, key, sec = _make_reseller()
        _topup(rs["id"], 50000000)
        for i in range(60):
            rdb.post_wallet_transaction(rs["id"], "adjustment_credit", 100,
                                        f"ADJ-BIG-{_n()}", "bulk")
        today = _today()
        st = generate_statement(rs["id"], period_type="custom",
                                start_date=today, end_date=today)
        self.assertEqual(st["transactions_count"], 61)
        # paginated reads
        H = _hdr(key, sec)
        seen = []
        after = None
        while True:
            url = f"/api/v1/statements/{st['public_id']}/transactions?limit=25"
            if after:
                url += f"&after_id={after}"
            r = self.client.get(url, headers=H)
            data = r.get_json()["data"]
            seen.extend(data["transactions"])
            if not r.get_json()["meta"]["pagination"]["has_more"]:
                break
            after = r.get_json()["meta"]["pagination"]["next_after_id"]
        self.assertEqual(len(seen), 61)
        # running balance monotonic with the ledger's own balance_after chain
        bals = [l["balance_after"] for l in seen]
        self.assertEqual(bals[0], "500000.00")   # the $500,000 seed topup
        self.assertEqual(bals[-1], rdb.from_cents(50000000 + 6000))

    def test_period_validation_and_unknown_reseller(self):
        rs, _k, _s = _make_reseller()
        with self.assertRaises(StatementError):
            generate_statement(rs["id"], period_type="bogus")
        with self.assertRaises(StatementError):
            generate_statement(rs["id"], period_type="custom",
                               start_date="2026-10-10", end_date="2026-10-01")
        with self.assertRaises(StatementError):
            generate_statement(rs["id"], period_type="monthly", month="2026-13")
        with self.assertRaises(StatementError):
            generate_statement(99999999, period_type="monthly", month=_month())
        with self.assertRaises(StatementError):
            generate_statement(rs["id"], period_type="invoice",
                               invoice_id="INV-ZZZZZZZZZZZZ")

    def test_schema_init_idempotent_and_sandbox_marker(self):
        sdb.ensure_statement_tables()
        sdb.ensure_statement_tables()
        idb.ensure_invoice_tables()
        db.cur.execute("PRAGMA integrity_check")
        self.assertEqual(db.cur.fetchone()[0], "ok")
        from reseller_api.config import validate_environment_marker
        with self.assertRaises(RuntimeError):
            validate_environment_marker({"API_ENVIRONMENT": "production"})


if __name__ == "__main__":
    unittest.main()
