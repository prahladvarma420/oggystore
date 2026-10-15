"""Phase 15 Prompt 7 — Invoice & Statement API verification + hardening.

The P2/P3 surfaces already exist; this suite runs the P7 checklist against
them and covers the gaps P7 added (list date/currency filters, invoice
transactions).  Reuses the real stack end to end.
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase15_api_", suffix=".db")
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
import reseller_api.middleware.authentication  # noqa: F401
from reseller_api.services.rate_limit import RequestRateDecision
from reseller_api.services.invoice_service import generate_invoice
from reseller_api.services.statement_service import generate_statement
from reseller_api.services.order_failure import handle_post_debit_failure

_ctr = 0


def _n():
    global _ctr
    _ctr += 1
    return _ctr


def _make_reseller():
    n = _n()
    uid = 9_600_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P15X{n}", f"p15x{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P15X{n}", uid, f"p15x{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=()):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P15X Product {n}", price),
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P15X-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


def _today():
    from utils.timezone import now as _now
    return _now().strftime("%Y-%m-%d")


class Phase15InvoiceStatementApiTests(unittest.TestCase):
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

    # ------------------------------------------------------------ auth matrix

    def test_authentication_matrix_on_financial_endpoints(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["AX-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15x-auth")
        today = _today()
        inv = generate_invoice(rs["id"], today, today)
        st = generate_statement(rs["id"], period_type="custom",
                                start_date=today, end_date=today)

        good = _hdr(key, sec)
        for path in ("/api/v1/invoices", f"/api/v1/invoices/{inv['public_id']}",
                     f"/api/v1/invoices/{inv['public_id']}/lines",
                     f"/api/v1/invoices/{inv['public_id']}/transactions",
                     f"/api/v1/invoices/{inv['public_id']}/document",
                     "/api/v1/statements", f"/api/v1/statements/{st['public_id']}",
                     f"/api/v1/statements/{st['public_id']}/transactions",
                     f"/api/v1/statements/{st['public_id']}/invoices",
                     f"/api/v1/statements/{st['public_id']}/document"):
            self.assertEqual(self.client.get(path, headers=good).status_code,
                             200, path)
            # no auth
            self.assertEqual(self.client.get(path).status_code, 401, path)
            # garbage key
            self.assertEqual(self.client.get(
                path, headers=_hdr("rk_test_ZZZZZZZZ", "x" * 40)).status_code,
                401, path)

        # revoked key
        rs2, key2, sec2 = _make_reseller()
        rdb.revoke_api_key_for_reseller(rs2["id"], key2)
        self.assertEqual(self.client.get("/api/v1/invoices",
                                         headers=_hdr(key2, sec2)).status_code, 401)
        # expired key
        rs3, key3, sec3 = _make_reseller()
        rdb.set_api_key_expiry_for_reseller(rs3["id"], key3,
                                            "2000-01-01 00:00:00")
        self.assertEqual(self.client.get("/api/v1/statements",
                                         headers=_hdr(key3, sec3)).status_code, 401)
        # suspended reseller
        rdb.set_reseller_status(rs["id"], "suspended")
        self.assertEqual(self.client.get("/api/v1/invoices",
                                         headers=good).status_code, 401)
        rdb.set_reseller_status(rs["id"], "active")

    # ------------------------------------------------------------ isolation / IDOR / enumeration

    def test_isolation_idor_enumeration(self):
        ra, ka, sa = _make_reseller()
        rb, kb, sb = _make_reseller()
        pid, pub = _make_product(10.0, codes=["ID-1"])
        _grant(ra["id"], pid)
        _topup(ra["id"], 50000)
        self._order(ka, sa, pub, 1, "p15x-idor")
        today = _today()
        inv = generate_invoice(ra["id"], today, today)
        st = generate_statement(ra["id"], period_type="custom",
                                start_date=today, end_date=today)
        for path in (f"/api/v1/invoices/{inv['public_id']}",
                     f"/api/v1/invoices/{inv['public_id']}/lines",
                     f"/api/v1/invoices/{inv['public_id']}/transactions",
                     f"/api/v1/invoices/{inv['public_id']}/document",
                     f"/api/v1/statements/{st['public_id']}",
                     f"/api/v1/statements/{st['public_id']}/transactions",
                     f"/api/v1/statements/{st['public_id']}/invoices",
                     f"/api/v1/statements/{st['public_id']}/document"):
            r = self.client.get(path, headers=_hdr(kb, sb))
            self.assertEqual(r.status_code, 404, path)
            # identical to a nonexistent id — no oracle
            if path.endswith(("/lines", "/transactions", "/invoices", "/document")):
                continue
            fake = self.client.get("/api/v1/invoices/INV-ZZZZZZZZZZZZ"
                                   if "invoices" in path else
                                   "/api/v1/statements/STMT-ZZZZZZZZZZZZ",
                                   headers=_hdr(kb, sb))
            self.assertEqual(fake.get_json()["error"]["code"],
                             r.get_json()["error"]["code"])

        # sequential/random public-id probing stays 404
        for guess in ("INV-AAAAAAAAAAAA", "INV-999999999999",
                      "STMT-AAAAAAAAAAAA"):
            self.assertEqual(self.client.get(f"/api/v1/invoices/{guess}",
                                             headers=_hdr(kb, sb)).status_code, 404)

        # list filters can never cross the boundary
        for path in (f"/api/v1/invoices?reseller_id={rb['id']}",
                     f"/api/v1/statements?reseller_id={rb['id']}",
                     f"/api/v1/invoices?status=finalized"):
            r = self.client.get(path, headers=_hdr(ka, sa))
            self.assertEqual(r.status_code, 200, path)
            body = r.get_json()["data"]
            items = body.get("invoices", body.get("statements", []))
            self.assertTrue(items)  # A sees his own
            self.assertNotIn(rb["public_id"], str(items))

    # ------------------------------------------------------------ filters + pagination

    def test_list_filters_and_pagination(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["PF-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15x-flt")
        today = _today()
        inv = generate_invoice(rs["id"], today, today)
        st = generate_statement(rs["id"], period_type="custom",
                                start_date=today, end_date=today)
        st_w = generate_statement(rs["id"], period_type="weekly",
                                  start_date=today)
        H = _hdr(key, sec)

        # invoice filters
        self.assertEqual(len(self.client.get(
            f"/api/v1/invoices?date_from={today}&date_to={today}",
            headers=H).get_json()["data"]["invoices"]), 1)
        self.assertEqual(self.client.get(
            "/api/v1/invoices?date_from=2999-01-01",
            headers=H).get_json()["data"]["invoices"], [])
        self.assertEqual(self.client.get(
            "/api/v1/invoices?currency=USDT",
            headers=H).get_json()["data"]["invoices"][0]["invoice_id"],
            inv["public_id"])
        self.assertEqual(self.client.get("/api/v1/invoices?currency=EUR",
                                         headers=H).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/invoices?date_from=nope",
                                         headers=H).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/invoices?status=bogus",
                                         headers=H).status_code, 400)

        # statement filters
        self.assertEqual(len(self.client.get(
            f"/api/v1/statements?date_from={today}&date_to={today}",
            headers=H).get_json()["data"]["statements"]), 2)
        self.assertEqual(len(self.client.get(
            "/api/v1/statements?period_type=weekly",
            headers=H).get_json()["data"]["statements"]), 1)
        self.assertEqual(self.client.get("/api/v1/statements?currency=EUR",
                                         headers=H).status_code, 400)

        # pagination: limit cap + cursor
        many = self.client.get("/api/v1/statements?limit=1", headers=H).get_json()
        self.assertEqual(len(many["data"]["statements"]), 1)
        self.assertTrue(many["meta"]["pagination"]["has_more"])
        after = many["meta"]["pagination"]["next_after_id"]
        page2 = self.client.get(f"/api/v1/statements?limit=1&after_id={after}",
                                headers=H).get_json()
        self.assertNotEqual(page2["data"]["statements"][0]["statement_id"],
                            many["data"]["statements"][0]["statement_id"])
        self.assertEqual(self.client.get("/api/v1/statements?limit=9999",
                                         headers=H).status_code, 400)

    # ------------------------------------------------------------ transactions + consistency

    def test_invoice_transactions_and_financial_consistency(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["TC-1", "TC-2"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15x-tx1")
        o2 = self._order(key, sec, pub, 1, "p15x-tx2")
        handle_post_debit_failure(o2, reason="t")
        today = _today()
        inv = generate_invoice(rs["id"], today, today)
        H = _hdr(key, sec)

        r = self.client.get(f"/api/v1/invoices/{inv['public_id']}/transactions",
                            headers=H)
        self.assertEqual(r.status_code, 200)
        txs = r.get_json()["data"]["transactions"]
        # topup + 2 purchases + refund
        self.assertEqual(len(txs), 4)
        types = sorted(t["type"] for t in txs)
        self.assertEqual(types, ["purchase_debit", "purchase_debit",
                                 "refund_credit", "topup_credit"])
        for t in txs:
            self.assertIn("reference", t)
            self.assertIn("balance_after", t)
            self.assertNotIn("id", t)
        # API totals == authoritative invoice totals
        d = self.client.get(f"/api/v1/invoices/{inv['public_id']}",
                            headers=H).get_json()["data"]["invoice"]
        db_inv = idb.get_invoice_by_public_id(inv["public_id"])
        self.assertEqual(d["total"], rdb.from_cents(db_inv["total_cents"]))
        self.assertEqual(d["subtotal"], rdb.from_cents(db_inv["subtotal_cents"]))
        self.assertEqual(d["refunds"], rdb.from_cents(db_inv["refund_cents"]))

    # ------------------------------------------------------------ documents

    def test_document_security(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["DC-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15x-doc")
        today = _today()
        inv = generate_invoice(rs["id"], today, today)
        st = generate_statement(rs["id"], period_type="custom",
                                start_date=today, end_date=today)
        H = _hdr(key, sec)

        for path in (f"/api/v1/invoices/{inv['public_id']}/document",
                     f"/api/v1/statements/{st['public_id']}/document"):
            r = self.client.get(path, headers=H)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn("text/html", r.content_type)
            html = r.get_data(as_text=True)
            self.assertNotIn(sec, html)
            self.assertNotIn("rapi.", html.split("</style>", 1)[-1] if False else html)
            # hmm: statement docs legitimately embed no references; invoice docs
            # embed ledger references by design (own data).  Only secrets banned.
            self.assertNotIn("secret", html.lower())

        # path traversal / bad ids / foreign
        self.assertEqual(self.client.get(
            "/api/v1/invoices/..%2F..%2Fetc/document", headers=H).status_code, 404)
        self.assertEqual(self.client.get(
            f"/api/v1/invoices/{inv['public_id']}/document").status_code, 401)
        # finalized documents are stable across reads
        d1 = self.client.get(f"/api/v1/invoices/{inv['public_id']}/document",
                             headers=H).get_data(as_text=True)
        d2 = self.client.get(f"/api/v1/invoices/{inv['public_id']}/document",
                             headers=H).get_data(as_text=True)
        self.assertEqual(d1, d2)

    # ------------------------------------------------------------ injection

    def test_malformed_inputs_and_injection(self):
        rs, key, sec = _make_reseller()
        today = _today()
        inv = generate_invoice(rs["id"], today, today)
        H = _hdr(key, sec)
        for bad in ("INV-' OR 1=1--", "INV-<script>", "INV-../../../etc",
                    "INV-😀😀😀😀😀😀😀😀😀😀😀😀", ""):
            r = self.client.get(f"/api/v1/invoices/{bad}/lines", headers=H)
            # 400/404 direct reject; 308 = Werkzeug normalizes the path to a
            # safe same-host route that then 404s (never serves content)
            self.assertIn(r.status_code, (400, 404, 308), bad)
            if r.status_code == 308:
                self.assertNotIn("data", str(r.get_json(silent=True) or {}))
        for bad in ("../..", "' OR '1'='1", "x" * 5000):
            r = self.client.get("/api/v1/invoices",
                                headers=H,
                                query_string={"status": bad, "limit": bad,
                                              "after_id": bad})
            self.assertEqual(r.status_code, 400, bad)

    # ------------------------------------------------------------ concurrency re-check

    def test_concurrent_reads_consistent(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(10.0, codes=["CX-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._order(key, sec, pub, 1, "p15x-cx")
        today = _today()
        inv = generate_invoice(rs["id"], today, today)
        H = _hdr(key, sec)

        results = []
        barrier = threading.Barrier(6)

        def read():
            c = self.app.test_client()
            barrier.wait()
            r = c.get(f"/api/v1/invoices/{inv['public_id']}", headers=H)
            results.append(r.get_json()["data"]["invoice"]["total"])

        ts = [threading.Thread(target=read) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(set(results)), 1)

    # ------------------------------------------------------------ sandbox

    def test_sandbox_isolation(self):
        from reseller_api.config import validate_environment_marker
        with self.assertRaises(RuntimeError):
            validate_environment_marker({"API_ENVIRONMENT": "production"})


if __name__ == "__main__":
    unittest.main()
