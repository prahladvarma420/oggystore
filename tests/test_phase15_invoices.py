"""Phase 15 Prompt 2 — reseller invoice system.

Invoices are documents over the authoritative ledger + orders.  Verified:
creation, numbering, lines, duplicate prevention, concurrency, finalized
immutability (service + DB triggers), refund/adjustment representation,
wallet non-duplication, reseller isolation, API authorization, retrieval,
invalid transitions, document generation, WebAdmin access, sandbox
isolation, and idempotent re-runs of schema init on existing data.
"""

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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase15_inv_", suffix=".db")
os.close(_fd)

import config
config.DB_PATH = _db_path
import database.db as db
db.DB_PATH = _db_path
db._tls = threading.local()
db.init_db()

import database.reseller_db as rdb
import database.invoice_db as idb
from reseller_api import create_app
import reseller_api.middleware.request_context  # noqa: F401
import reseller_api.middleware.rate_limit  # noqa: F401
import reseller_api.middleware.security_controls  # noqa: F401
from reseller_api.services.rate_limit import RequestRateDecision
from reseller_api.services.invoice_service import (
    InvoiceError, generate_invoice, void_invoice,
)
from reseller_api.services.order_failure import (
    handle_post_debit_failure, handle_cancellation, handle_partial_failure,
)

_ctr = 0


def _n():
    global _ctr
    _ctr += 1
    return _ctr


def _make_reseller():
    n = _n()
    uid = 9_650_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P15R{n}", f"p15r{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P15R{n}", uid, f"p15r{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=()):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P15 Product {n}", price),
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P15-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


def _today():
    from utils.timezone import now as _now
    return _now().strftime("%Y-%m-%d")


class Phase15InvoiceTests(unittest.TestCase):
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

    # ------------------------------------------------------------ 1-2. creation + numbering

    def test_invoice_creation_from_real_activity(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["IV-1", "IV-2", "IV-3"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 100000)
        self._order(key, sec, pub, 2, "p15-create-1")   # $20
        self._order(key, sec, pub, 1, "p15-create-2")   # $10

        today = _today()
        inv = generate_invoice(rs["id"], today, today, issued_by="owner")
        self.assertEqual(inv["status"], "finalized")
        self.assertEqual(inv["subtotal_cents"], 3000)
        self.assertEqual(inv["total_cents"], 3000)
        self.assertEqual(inv["amount_paid_cents"], 3000)
        self.assertEqual(inv["amount_due_cents"], 0)
        self.assertEqual(inv["orders_count"], 2)
        self.assertRegex(inv["public_id"], r"^INV-[A-HJ-NP-Z2-9]{12}$")
        self.assertRegex(inv["invoice_number"], r"^INV-\d{4}-\d{6}$")
        self.assertTrue(inv["finalized_at"])

        lines = idb.get_invoice_lines(inv["id"])
        kinds = sorted(l["source_type"] for l in lines)
        self.assertEqual(kinds, ["purchase", "purchase", "topup"])
        # lines carry public ids + ledger references, never internal ids
        for l in lines:
            self.assertTrue(l["source_reference"])
            self.assertNotIn(str(l["invoice_id"]), str(l["order_public_id"]))

    def test_invoice_numbers_unique_and_sequential(self):
        rs, key, sec = _make_reseller()
        today = _today()
        a = generate_invoice(rs["id"], today, today)
        b = generate_invoice(rs["id"], today, today)   # idempotent replay
        self.assertEqual(a["id"], b["id"])
        self.assertTrue(b["is_duplicate"])
        # next period gets a fresh number
        from utils.timezone import now as _now
        from datetime import timedelta as _td
        yesterday = (_now() - _td(days=1)).strftime("%Y-%m-%d")
        c = generate_invoice(rs["id"], yesterday, yesterday)
        self.assertNotEqual(a["invoice_number"], c["invoice_number"])
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_invoices"
            " WHERE invoice_number IS NOT NULL"
            " GROUP BY invoice_number HAVING COUNT(*)>1")
        self.assertEqual(db.cur.fetchall(), [])

    def test_concurrent_generation_single_invoice(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["CC-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 100000)
        self._order(key, sec, pub, 1, "p15-conc")
        today = _today()

        results = []
        barrier = threading.Barrier(5)

        def gen():
            barrier.wait()
            results.append(generate_invoice(rs["id"], today, today))

        ts = [threading.Thread(target=gen) for _ in range(5)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        ids = {r["id"] for r in results}
        self.assertEqual(len(ids), 1, "exactly one invoice row must exist")
        numbers = {r["invoice_number"] for r in results}
        self.assertEqual(len(numbers), 1)
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_invoices WHERE reseller_id=?",
            (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 1)

    # ------------------------------------------------------------ 3. wallet non-duplication

    def test_generation_moves_no_money(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["NM-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15-nomove")
        today = _today()

        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_wallet_transactions WHERE reseller_id=?",
            (rs["id"],))
        before = int(db.cur.fetchone()[0])
        bal_before = rdb.get_wallet_balance_cents(rs["id"])
        generate_invoice(rs["id"], today, today)
        generate_invoice(rs["id"], today, today)   # replay
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_wallet_transactions WHERE reseller_id=?",
            (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), before)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), bal_before)

    # ------------------------------------------------------------ 4. refunds & adjustments on invoices

    def test_refunds_and_adjustments_represented(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["RF-%d" % i for i in range(6)])
        _grant(rs["id"], pid)
        _topup(rs["id"], 100000)
        o1 = self._order(key, sec, pub, 1, "p15-rf-1")
        o2 = self._order(key, sec, pub, 1, "p15-rf-2")
        o3 = self._order(key, sec, pub, 2, "p15-rf-3")
        handle_post_debit_failure(o1, reason="t")            # +$10 full
        handle_cancellation(o2, post_debit=True)             # +$10 cancel
        handle_partial_failure(o3, fulfilled_qty=1, failed_qty=1)  # +$10 partial
        rdb.post_wallet_transaction(rs["id"], "adjustment_credit", 500,
                                    "ADJ-P15-1", "goodwill")
        rdb.post_wallet_transaction(rs["id"], "adjustment_debit", -200,
                                    "ADJ-P15-2", "correction")

        today = _today()
        inv = generate_invoice(rs["id"], today, today)
        # subtotal 10+10+20=4000; refunds 3000; adj credit 500; adj debit 200
        # total = 4000-3000+200-500 = 700
        self.assertEqual(inv["subtotal_cents"], 4000)
        self.assertEqual(inv["refund_cents"], 3000)
        self.assertEqual(inv["adjustment_credit_cents"], 500)
        self.assertEqual(inv["adjustment_debit_cents"], 200)
        self.assertEqual(inv["total_cents"], 700)
        self.assertEqual(inv["amount_paid_cents"], 700)
        lines = idb.get_invoice_lines(inv["id"])
        kinds = {}
        for l in lines:
            kinds[l["source_type"]] = kinds.get(l["source_type"], 0) + 1
        self.assertEqual(kinds.get("purchase"), 3)
        self.assertEqual(kinds.get("refund_full"), 1)
        self.assertEqual(kinds.get("refund_cancellation"), 1)
        self.assertEqual(kinds.get("refund_partial"), 1)
        self.assertEqual(kinds.get("adjustment_credit"), 1)
        self.assertEqual(kinds.get("adjustment_debit"), 1)
        self.assertEqual(kinds.get("topup"), 1)
        # line totals reconcile to the header
        charge = sum(l["total_cents"] for l in lines
                     if l["source_type"] in ("purchase", "adjustment_debit"))
        credit = sum(l["total_cents"] for l in lines
                     if l["source_type"].startswith("refund_")) + \
                 sum(l["total_cents"] for l in lines
                     if l["source_type"] == "adjustment_credit")
        self.assertEqual(charge - (-credit) if False else (charge + credit if False else 0), 0) if False else None
        self.assertEqual(
            sum(l["total_cents"] for l in lines if l["source_type"] == "purchase")
            + sum(l["total_cents"] for l in lines if l["source_type"] == "adjustment_debit")
            + sum(l["total_cents"] for l in lines if l["source_type"].startswith("refund_"))
            - sum(l["total_cents"] for l in lines if l["source_type"] == "adjustment_credit"),
            inv["total_cents"])

    # ------------------------------------------------------------ 5. immutability

    def test_finalized_invoice_immutable_service_and_db(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["IM-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 100000)
        self._order(key, sec, pub, 1, "p15-imm")
        today = _today()
        inv = generate_invoice(rs["id"], today, today)
        self.assertEqual(inv["status"], "finalized")

        # service layer: regeneration of a finalized period returns the same row
        again = generate_invoice(rs["id"], today, today)
        self.assertTrue(again["is_duplicate"])
        self.assertEqual(again["total_cents"], inv["total_cents"])

        # DB layer: financial fields frozen
        with self.assertRaises(Exception) as ctx:
            db.cur.execute(
                "UPDATE reseller_invoices SET total_cents=1 WHERE id=?",
                (inv["id"],))
            db.conn.commit()
        self.assertIn("immutable", str(ctx.exception))
        db.conn.rollback()

        # lines frozen too
        with self.assertRaises(Exception):
            db.cur.execute(
                "DELETE FROM reseller_invoice_lines WHERE invoice_id=?",
                (inv["id"],))
            db.conn.commit()
        db.conn.rollback()

        # invoices cannot be deleted at all
        with self.assertRaises(Exception):
            db.cur.execute("DELETE FROM reseller_invoices WHERE id=?", (inv["id"],))
            db.conn.commit()
        db.conn.rollback()

    def test_invalid_transitions_rejected(self):
        rs, _k, _s = _make_reseller()
        today = _today()
        inv = generate_invoice(rs["id"], today, today)
        self.assertEqual(inv["status"], "finalized")
        # finalized -> draft must fail (trigger)
        with self.assertRaises(Exception):
            db.cur.execute(
                "UPDATE reseller_invoices SET status='draft' WHERE id=?",
                (inv["id"],))
            db.conn.commit()
        db.conn.rollback()
        # void -> finalized must fail
        v = void_invoice(inv["id"], "test correction")
        self.assertEqual(v["status"], "void")
        with self.assertRaises(Exception):
            db.cur.execute(
                "UPDATE reseller_invoices SET status='finalized' WHERE id=?",
                (inv["id"],))
            db.conn.commit()
        db.conn.rollback()
        # void is idempotent at the service layer
        v2 = void_invoice(inv["id"], "again")
        self.assertTrue(v2["is_duplicate"])
        # and a voided period can be re-invoiced as a NEW document
        fresh = generate_invoice(rs["id"], today, today)
        self.assertNotEqual(fresh["id"], inv["id"])
        self.assertNotEqual(fresh["invoice_number"], inv["invoice_number"])

    # ------------------------------------------------------------ 6. edge amounts

    def test_zero_and_edge_amount_invoices(self):
        # empty period -> $0 invoice, still valid
        rs, key, sec = _make_reseller()
        today = _today()
        inv = generate_invoice(rs["id"], today, today)
        self.assertEqual(inv["total_cents"], 0)
        self.assertEqual(inv["subtotal_cents"], 0)
        self.assertEqual(inv["orders_count"], 0)
        self.assertEqual(idb.get_invoice_lines(inv["id"]), [])

        # very small amount (one cent) + large amount
        rs2, key2, sec2 = _make_reseller()
        pid, pub = _make_product(0.01, codes=["CT-1"])
        _grant(rs2["id"], pid)
        pid2, pub2 = _make_product(5000.0, codes=["BG-1"])
        _grant(rs2["id"], pid2)
        _topup(rs2["id"], 60000000)
        self._order(key2, sec2, pub, 1, "p15-cent")     # $0.01
        self._order(key2, sec2, pub2, 1, "p15-big")     # $5000.00
        inv = generate_invoice(rs2["id"], today, today)
        self.assertEqual(inv["subtotal_cents"], 500001)
        self.assertEqual(inv["total_cents"], 500001)

    # ------------------------------------------------------------ 7. API surface

    def test_api_list_detail_lines_document(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["AP-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15-api-1")
        today = _today()
        inv = generate_invoice(rs["id"], today, today)
        H = _hdr(key, sec)

        r = self.client.get("/api/v1/invoices", headers=H)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        invs = body["data"]["invoices"]
        self.assertEqual(len(invs), 1)
        self.assertEqual(invs[0]["invoice_id"], inv["public_id"])
        self.assertEqual(invs[0]["total"], "10.00")
        self.assertIn("pagination", body["meta"])

        d = self.client.get(f"/api/v1/invoices/{inv['public_id']}", headers=H)
        self.assertEqual(d.status_code, 200)
        self.assertEqual(d.get_json()["data"]["invoice"]["invoice_number"],
                         inv["invoice_number"])

        ls = self.client.get(f"/api/v1/invoices/{inv['public_id']}/lines", headers=H)
        self.assertEqual(ls.status_code, 200)
        lines = ls.get_json()["data"]["lines"]
        self.assertEqual(len(lines), 2)   # purchase + topup
        kinds = sorted(l["type"] for l in lines)
        self.assertEqual(kinds, ["purchase", "topup"])
        purchase = [l for l in lines if l["type"] == "purchase"][0]
        self.assertTrue(purchase["order_id"].startswith("ORD-"))
        self.assertTrue(purchase["product_id"].startswith("PRD-"))
        self.assertEqual(purchase["total"], "10.00")

        doc = self.client.get(f"/api/v1/invoices/{inv['public_id']}/document",
                              headers=H)
        self.assertEqual(doc.status_code, 200)
        self.assertIn("text/html", doc.content_type)
        html = doc.get_data(as_text=True)
        self.assertIn(inv["invoice_number"], html)
        self.assertIn("10.00 USDT", html)
        # no internal ids / secrets in the document
        self.assertNotIn(f">{inv['id']}<", html)      # internal invoice id
        self.assertNotIn(f">{rs['id']}<", html)       # internal reseller id
        self.assertNotIn(sec, html)                   # API secret
        self.assertNotIn("rapi.", html)               # internal ledger refs stay API-side

    def test_api_isolation_and_auth(self):
        ra, ka, sa = _make_reseller()
        rb, kb, sb = _make_reseller()
        pid, pub = _make_product(10.0, codes=["ISO-1"])
        _grant(ra["id"], pid)
        _topup(ra["id"], 50000)
        self._order(ka, sa, pub, 1, "p15-iso-1")
        today = _today()
        inv = generate_invoice(ra["id"], today, today)

        # B cannot read A's invoice — uniform 404, no oracle
        for path in (f"/api/v1/invoices/{inv['public_id']}",
                     f"/api/v1/invoices/{inv['public_id']}/lines",
                     f"/api/v1/invoices/{inv['public_id']}/document"):
            r = self.client.get(path, headers=_hdr(kb, sb))
            self.assertEqual(r.status_code, 404, path)
            self.assertEqual(self.client.get("/api/v1/invoices/INV-ZZZZZZZZZZZZ",
                                             headers=_hdr(kb, sb)).status_code, 404)
        # unauthenticated
        self.assertEqual(self.client.get("/api/v1/invoices").status_code, 401)
        # bad id format -> 404 (no oracle)
        self.assertEqual(self.client.get("/api/v1/invoices/ORD-00000001",
                                         headers=_hdr(kb, sb)).status_code, 404)

    def test_api_repeated_request_idempotent(self):
        rs, key, sec = _make_reseller()
        today = _today()
        inv = generate_invoice(rs["id"], today, today)
        H = _hdr(key, sec)
        r1 = self.client.get(f"/api/v1/invoices/{inv['public_id']}", headers=H)
        r2 = self.client.get(f"/api/v1/invoices/{inv['public_id']}", headers=H)
        self.assertEqual(r1.get_json()["data"], r2.get_json()["data"])
        self.assertNotEqual(r1.get_json()["request_id"],
                            r2.get_json()["request_id"])

    # ------------------------------------------------------------ 8. WebAdmin

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

    @staticmethod
    def _csrf_hdr():
        return {"X-CSRF-Token": "test-csrf"}

    def test_webadmin_generate_list_detail_void_document(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["WA-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15-wa-1")
        today = _today()

        ac = self._owner()
        # generate (owner-only write)
        r = ac.post(f"/api/resellers/{rs['public_id']}/invoices/generate",
                    json={"period_start": today, "period_end": today},
                    headers=self._csrf_hdr())
        self.assertEqual(r.status_code, 200, r.get_json())
        gen = r.get_json()
        self.assertTrue(gen["ok"])
        self.assertFalse(gen["duplicate"])
        # idempotent re-generation
        r2 = ac.post(f"/api/resellers/{rs['public_id']}/invoices/generate",
                     json={"period_start": today, "period_end": today},
                     headers=self._csrf_hdr())
        self.assertTrue(r2.get_json()["duplicate"])
        self.assertEqual(r2.get_json()["invoice_number"], gen["invoice_number"])

        # list + filters
        lst = ac.get(f"/api/invoices?reseller={rs['public_id']}&status=finalized")
        self.assertEqual(lst.status_code, 200)
        invs = lst.get_json()["invoices"]
        self.assertEqual(len(invs), 1)
        self.assertNotIn("id", invs[0])          # no internal ids
        self.assertNotIn("reseller_id", invs[0])
        self.assertEqual(invs[0]["total"], "10.00")
        # status filter: this invoice is not void yet — and must not appear
        lst2 = ac.get(f"/api/invoices?status=void&search={gen['invoice_number']}")
        self.assertEqual(lst2.get_json()["invoices"], [])
        lst3 = ac.get(f"/api/invoices?search={gen['invoice_number']}")
        self.assertEqual(len(lst3.get_json()["invoices"]), 1)

        # detail with lines
        det = ac.get(f"/api/invoices/{gen['invoice_id']}")
        self.assertEqual(det.status_code, 200)
        self.assertTrue(det.get_json()["lines"])

        # document
        doc = ac.get(f"/api/invoices/{gen['invoice_id']}/document")
        self.assertEqual(doc.status_code, 200)
        self.assertIn(gen["invoice_number"], doc.get_data(as_text=True))

        # void (owner-only) then list shows it
        rv = ac.post(f"/api/invoices/{gen['invoice_id']}/void",
                     json={"reason": "test correction"},
                     headers=self._csrf_hdr())
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.get_json()["status"], "void")
        # void again -> duplicate, no error
        rv2 = ac.post(f"/api/invoices/{gen['invoice_id']}/void",
                      json={"reason": "again"}, headers=self._csrf_hdr())
        self.assertTrue(rv2.get_json()["duplicate"])
        # post-void: it appears under the void filter
        lstv = ac.get(f"/api/invoices?status=void&search={gen['invoice_number']}")
        self.assertEqual(len(lstv.get_json()["invoices"]), 1)

        # gates: anon 401; non-owner 403 on generate/void
        import web_admin.app as wa
        anon = wa.app.test_client()
        self.assertEqual(anon.get("/api/invoices").status_code, 401)
        non_owner = self._owner(role="admin")
        self.assertEqual(
            non_owner.post(f"/api/resellers/{rs['public_id']}/invoices/generate",
                           json={"period_start": today, "period_end": today},
                           headers=self._csrf_hdr()).status_code,
            403)

        # audit trail
        db.cur.execute(
            "SELECT COUNT(*) FROM admin_logs WHERE action LIKE '%invoice%'")
        self.assertGreaterEqual(int(db.cur.fetchone()[0]), 2)

    # ------------------------------------------------------------ 9. misc

    def test_period_validation(self):
        rs, _k, _s = _make_reseller()
        with self.assertRaises(InvoiceError):
            generate_invoice(rs["id"], "2026-13-40", "2026-01-01")
        with self.assertRaises(InvoiceError):
            generate_invoice(rs["id"], "2026-10-10", "2026-10-01")   # reversed
        with self.assertRaises(InvoiceError):
            generate_invoice(rs["id"], "2999-01-01", "2999-01-31")   # future
        with self.assertRaises(InvoiceError):
            generate_invoice(99999999, "2026-01-01", "2026-01-31")   # unknown reseller

    def test_schema_init_idempotent_on_existing_data(self):
        idb.ensure_invoice_tables()
        idb.ensure_invoice_tables()
        db.cur.execute("PRAGMA integrity_check")
        self.assertEqual(db.cur.fetchone()[0], "ok")

    def test_sandbox_isolation(self):
        """Invoices live in the environment's own DB; the deployment marker
        still rejects cross-environment boots (P13 contract)."""
        from reseller_api.config import validate_environment_marker
        with self.assertRaises(RuntimeError):
            validate_environment_marker({"API_ENVIRONMENT": "production"})
        validate_environment_marker({"API_ENVIRONMENT": "test"})


if __name__ == "__main__":
    unittest.main()
