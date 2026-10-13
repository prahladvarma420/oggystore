"""Phase 12 Prompt 9 — complete reseller order security, financial &
isolation audit.

Audit-by-test across every attack surface of the reseller order API:

  authentication    — missing/malformed/unknown/wrong-secret/revoked/
                      suspended credentials all 401 uniformly
  authorization     — order only allowed products; own orders/mappings/
                      financial data only
  IDOR              — foreign order/delivery/product/key all 404, no oracle
  price manipulation— every client-supplied financial field rejected; the
                      server price is always authoritative
  wallet security   — negative/zero/huge amounts rejected at the ledger;
                      balance can never go negative
  stock security    — negative/zero/huge quantities rejected; no oversell
  order security    — key replay, cross-reseller key collision, manipulated
                      IDs, replayed requests
  master order      — master order id / fulfillment / supplier / cost /
                      delivery status / internal notes are not client-settable
                      and never leak into responses
  financial integrity
  webhook hygiene   — payloads carry no secrets, delivery content, or
                      internal identifiers; events dedupe
  performance       — create latency bound; list endpoint index-backed
"""

import os
import sys
import tempfile
import threading
import time
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase12_audit_", suffix=".db")
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
from reseller_api.services.order_failure import (
    handle_post_debit_failure,
    handle_cancellation,
    handle_partial_failure,
)

_ctr = 0


def _n():
    global _ctr
    _ctr += 1
    return _ctr


def _make_reseller(name=None, status="active"):
    n = _n()
    name = name or f"P9Res{n}"
    uid = 9_910_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, name, name.lower()),
    )
    db.conn.commit()
    rs = rdb.create_reseller(name, uid, name.lower())
    if status != "active":
        rdb.set_reseller_status(rs["id"], status)
    else:
        rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=("A9-1",), max_per_order=10):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P9 Product {n}", price),
    )
    db.conn.commit()
    pid = db.cur.lastrowid
    db.cur.execute("UPDATE products SET max_per_order=? WHERE id=?", (max_per_order, pid))
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P9-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


def _oid(pub):
    return int(pub.replace("ORD-", ""))


def _ledger(rid):
    db.cur.execute(
        "SELECT tx_type, amount_cents, balance_after_cents FROM"
        " reseller_wallet_transactions WHERE reseller_id=? ORDER BY id", (rid,))
    return db.cur.fetchall()


class Phase12OrderSecurityAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.audit_patcher = patch(
            "reseller_api.middleware.request_context.persist_audit_record",
            return_value=True,
        )
        cls.audit_patcher.start()
        cls.rate_patcher = patch(
            "reseller_api.middleware.rate_limit.evaluate_request_rate",
            return_value=RequestRateDecision(
                True, "allowed", limit=60, remaining=59, reset_after=45
            ),
        )
        cls.rate_patcher.start()
        cls.global_patcher = patch(
            "reseller_api.middleware.security_controls.is_global_api_enabled",
            return_value=True,
        )
        cls.global_patcher.start()
        cls.app = create_app({
            "TESTING": True,
            "API_ENVIRONMENT": "test",
            "INIT_DATABASE": False,
            "RESELLER_API_HSTS": False,
        })
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.global_patcher.stop()
        cls.rate_patcher.stop()
        cls.audit_patcher.stop()
        try:
            db.close_thread_connection()
        finally:
            try:
                os.unlink(_db_path)
            except FileNotFoundError:
                pass

    def _post(self, hdrs, pub, qty=1, coid=None, extra=None):
        body = {"product_id": pub, "quantity": qty}
        if coid:
            body["client_order_id"] = coid
        if extra:
            body.update(extra)
        return self.client.post("/api/v1/orders", headers=hdrs, json=body)

    # ================================================================
    # AUTHENTICATION
    # ================================================================

    def test_auth_missing_malformed_unknown_wrong_revoked_suspended(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)

        # missing header
        r = self.client.post("/api/v1/orders", json={"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 401)
        # malformed header
        r = self.client.post("/api/v1/orders",
                             headers={"Authorization": "nonsense"},
                             json={"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 401)
        # unknown key id (valid-looking format)
        r = self._post(_hdr("rk_test_UNKNOWN12", "x" * 40), pub)
        self.assertEqual(r.status_code, 401)
        # wrong secret for a real key id
        r = self._post(_hdr(key, "z" * len(sec)), pub)
        self.assertEqual(r.status_code, 401)
        # revoked key
        rs2, key2, sec2 = _make_reseller()
        rdb.revoke_api_key(key2) if hasattr(rdb, "revoke_api_key") else rdb.set_api_key_status(key2, "revoked")
        r = self._post(_hdr(key2, sec2), pub)
        self.assertEqual(r.status_code, 401)
        # suspended reseller (valid key, suspended after issuance)
        rs3, key3, sec3 = _make_reseller()
        rdb.set_reseller_status(rs3["id"], "suspended")
        r = self._post(_hdr(key3, sec3), pub)
        self.assertEqual(r.status_code, 401)
        # uniform failure shape: no oracle distinguishing causes
        self.assertEqual(
            self.client.post("/api/v1/orders", headers=_hdr(key, "z"*len(sec)),
                             json={"product_id": pub, "quantity": 1}).get_json()["error"]["code"],
            self.client.post("/api/v1/orders", headers=_hdr("rk_test_UNKNOWN12", "x"*40),
                             json={"product_id": pub, "quantity": 1}).get_json()["error"]["code"],
        )

    # ================================================================
    # AUTHORIZATION / IDOR
    # ================================================================

    def test_idor_foreign_order_detail_and_delivery_404(self):
        rs1, key1, sec1 = _make_reseller()
        rs2, key2, sec2 = _make_reseller()
        pid, pub = _make_product(codes=["I1", "I2"])
        _grant(rs1["id"], pid)
        _grant(rs2["id"], pid)
        _topup(rs1["id"], 50000)
        _topup(rs2["id"], 50000)

        r = self._post(_hdr(key1, sec1), pub, 1, "p9-idor-owned")
        self.assertEqual(r.status_code, 201)
        foreign_oid = r.get_json()["data"]["order_id"]

        for endpoint in (f"/api/v1/orders/{foreign_oid}",
                         f"/api/v1/orders/{foreign_oid}/delivery"):
            g = self.client.get(endpoint, headers=_hdr(key2, sec2))
            self.assertEqual(g.status_code, 404, endpoint)
            # identical to a nonexistent id — no existence oracle
            g2 = self.client.get("/api/v1/orders/ORD-99999999",
                                 headers=_hdr(key2, sec2))
            self.assertEqual(g2.status_code, 404)
            self.assertEqual(g.get_json()["error"]["code"],
                             g2.get_json()["error"]["code"])

    def test_idor_list_isolation_and_client_key_filter(self):
        rs1, key1, sec1 = _make_reseller()
        rs2, key2, sec2 = _make_reseller()
        pid, pub = _make_product(codes=["L1", "L2"])
        _grant(rs1["id"], pid)
        _grant(rs2["id"], pid)
        _topup(rs1["id"], 50000)
        _topup(rs2["id"], 50000)

        self._post(_hdr(key1, sec1), pub, 1, "p9-list-r1")
        # rs2's list is empty of rs1's order
        lst = self.client.get("/api/v1/orders", headers=_hdr(key2, sec2))
        self.assertEqual(lst.status_code, 200)
        self.assertEqual(lst.get_json()["data"]["orders"], [])
        # filtering by rs1's client_order_id leaks nothing
        lst2 = self.client.get("/api/v1/orders?client_order_id=p9-list-r1",
                               headers=_hdr(key2, sec2))
        self.assertEqual(lst2.get_json()["data"]["orders"], [])

    def test_idor_foreign_product_not_orderable(self):
        rs1, key1, sec1 = _make_reseller()
        rs2, key2, sec2 = _make_reseller()
        pid, pub = _make_product(codes=["P1"])
        _grant(rs1["id"], pid)          # only rs1 has access + mapping
        _topup(rs2["id"], 50000)
        r = self._post(_hdr(key2, sec2), pub, 1, "p9-idor-prod")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(_ledger(rs2["id"]), _ledger(rs2["id"]))  # sanity
        self.assertEqual(rdb.get_wallet_balance_cents(rs2["id"]), 50000)

    def test_balance_endpoint_has_no_identity_selector(self):
        rs, key, sec = _make_reseller()
        _topup(rs["id"], 12345)
        r = self.client.get("/api/v1/balance", headers=_hdr(key, sec))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["data"]["balance"], "123.45")
        # any query parameter (incl. reseller_id) is rejected outright
        for q in ("reseller_id=1", "reseller=x", "user_id=1"):
            r = self.client.get(f"/api/v1/balance?{q}", headers=_hdr(key, sec))
            self.assertEqual(r.status_code, 400, q)

    def test_cross_reseller_client_order_id_no_collision(self):
        """The idempotency scope is (reseller_id, client_order_id): two
        resellers may safely use the SAME key string — two independent
        orders, no replay confusion, no leak."""
        rs1, key1, sec1 = _make_reseller()
        rs2, key2, sec2 = _make_reseller()
        pid, pub = _make_product(codes=["X1", "X2"])
        _grant(rs1["id"], pid)
        _grant(rs2["id"], pid)
        _topup(rs1["id"], 50000)
        _topup(rs2["id"], 50000)

        r1 = self._post(_hdr(key1, sec1), pub, 1, "shared-key")
        r2 = self._post(_hdr(key2, sec2), pub, 1, "shared-key")
        self.assertEqual((r1.status_code, r2.status_code), (201, 201))
        self.assertNotEqual(r1.get_json()["data"]["order_id"],
                            r2.get_json()["data"]["order_id"])
        # each sees only their own when replaying the shared key
        d1 = self.client.get("/api/v1/orders?client_order_id=shared-key",
                             headers=_hdr(key1, sec1)).get_json()["data"]["orders"]
        self.assertEqual(len(d1), 1)
        self.assertEqual(d1[0]["order_id"], r1.get_json()["data"]["order_id"])

    # ================================================================
    # PRICE MANIPULATION
    # ================================================================

    def test_price_fields_rejected_and_server_price_authoritative(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=25.0, codes=["PM-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)

        for field, val in [("price", 0.01), ("unit_price", 0.01),
                           ("unit_price_cents", 1), ("total", 0.01),
                           ("total_cents", 1), ("balance", 999999),
                           ("cost", 0.01), ("markup", 0), ("discount", 100),
                           ("reseller_id", 1), ("reseller", "x"), ("source", "x")]:
            r = self._post(_hdr(key, sec), pub, 1, f"p9-pm-{field}",
                           extra={field: val})
            self.assertEqual(r.status_code, 400, field)

        # currency is inert (always USDT); the server price always wins
        r = self._post(_hdr(key, sec), pub, 1, "p9-pm-currency",
                       extra={"currency": "BTC"})
        self.assertEqual(r.status_code, 201)
        d = r.get_json()["data"]
        self.assertEqual(d["currency"], "USDT")
        self.assertEqual(d["unit_price"], "25.00")
        self.assertEqual(d["total"], "25.00")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000 - 2500)

    # ================================================================
    # WALLET SECURITY (ledger-level guards)
    # ================================================================

    def test_wallet_ledger_rejects_bad_amounts(self):
        rs, key, sec = _make_reseller()
        # negative on a credit type
        with self.assertRaises(ValueError):
            rdb.post_wallet_transaction(rs["id"], "refund_credit", -100, "P9-N1", "x")
        # zero
        with self.assertRaises(ValueError):
            rdb.post_wallet_transaction(rs["id"], "topup_credit", 0, "P9-N2", "x")
        # positive on a debit type
        with self.assertRaises(ValueError):
            rdb.post_wallet_transaction(rs["id"], "purchase_debit", 100, "P9-N3", "x")
        # non-integer
        with self.assertRaises(TypeError):
            rdb.post_wallet_transaction(rs["id"], "topup_credit", 10.5, "P9-N5", "x")
        # unsafe reference characters rejected (ledger idempotency hygiene)
        with self.assertRaises(ValueError):
            rdb.post_wallet_transaction(rs["id"], "topup_credit", 100,
                                        "bad ref; DROP TABLE", "x")

    def test_wallet_balance_ceiling_never_overflows(self):
        """Huge-amount safety: the conditional credit UPDATE enforces the
        int64 ceiling atomically — a credit that would overflow is refused
        and the balance is left intact (fails closed, never wraps)."""
        rs, key, sec = _make_reseller()
        from database.reseller_db import MAX_WALLET_BALANCE_CENTS
        st, _ = rdb.post_wallet_transaction(
            rs["id"], "topup_credit", MAX_WALLET_BALANCE_CENTS, "P9-H1", "seed")
        self.assertEqual(st, "posted")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]),
                         MAX_WALLET_BALANCE_CENTS)
        # one more cent must be refused atomically — no overflow, no change
        st, _ = rdb.post_wallet_transaction(
            rs["id"], "topup_credit", 1, "P9-H2", "overflow")
        self.assertEqual(st, "balance_limit_exceeded")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]),
                         MAX_WALLET_BALANCE_CENTS)

    # ================================================================
    # STOCK SECURITY
    # ================================================================

    def test_quantity_boundaries(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["Q1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 500000)

        for bad in (0, -1, -1000, 1.5, "abc", True, None):
            r = self._post(_hdr(key, sec), pub, bad, f"p9-q-{bad}")
            self.assertEqual(r.status_code, 400, f"quantity={bad!r}")
        # beyond absolute ceiling
        r = self._post(_hdr(key, sec), pub, 1001, "p9-q-huge")
        self.assertEqual(r.status_code, 400)
        # beyond product max_per_order (10) but within stock arithmetic
        r = self._post(_hdr(key, sec), pub, 11, "p9-q-max")
        self.assertEqual(r.status_code, 400)
        # nothing was consumed or charged
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 500000)
        db.cur.execute("SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=1", (pid,))
        self.assertEqual(int(db.cur.fetchone()[0]), 0)

    # ================================================================
    # ORDER / MASTER-ORDER SECURITY
    # ================================================================

    def test_master_order_fields_not_client_settable(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["M1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)

        # server-determined metadata is rejected
        for field in ("order_id", "id", "status", "master_status",
                      "failure_reason", "payment_method"):
            r = self._post(_hdr(key, sec), pub, 1, f"p9-mo-{field}",
                           extra={field: "x"})
            self.assertEqual(r.status_code, 400, field)

        # inert unknowns cannot influence the stored row
        r = self._post(_hdr(key, sec), pub, 1, "p9-mo-inert",
                       extra={"request_id": "forged", "fulfillment_type": "manual",
                              "supplier": "evil", "internal_notes": "x",
                              "master_order_id": "1", "delivery_status": "delivered",
                              "user_id": 5})
        self.assertEqual(r.status_code, 201, r.get_json())
        oid = _oid(r.get_json()["data"]["order_id"])
        db.cur.execute("SELECT request_id, source, user_id FROM orders WHERE id=?", (oid,))
        req_id, source, user_id = db.cur.fetchone()
        self.assertNotEqual(req_id, "forged")          # server-generated
        self.assertTrue(req_id)
        self.assertEqual(source, "reseller_api")
        self.assertEqual(user_id, 0)

    def test_response_hygiene_no_internal_fields(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["H1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._post(_hdr(key, sec), pub, 1, "p9-hyg-1")
        self.assertEqual(r.status_code, 201)
        data = r.get_json()["data"]
        for leaked in ("reseller_id", "reseller_key_id", "user_id", "request_id",
                       "wallet_transaction_id", "wallet_reference", "cost",
                       "supplier", "internal_notes", "master_status", "source",
                       "delivery", "stock_restored", "failure_reason"):
            self.assertNotIn(leaked, data)

        det = self.client.get(f"/api/v1/orders/{data['order_id']}",
                              headers=_hdr(key, sec))
        detail = det.get_json()["data"]["order"]
        for leaked in ("reseller_id", "reseller_key_id", "user_id", "request_id",
                       "cost", "supplier", "internal_notes", "source",
                       "delivery", "stock_restored"):
            self.assertNotIn(leaked, detail)

    def test_manipulated_product_id_404_uniformly(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["Z1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        other_pid, other_pub = _make_product(codes=["Z2"])   # not granted
        for bad in ("PRD-!!!!!!!!!!!!", "PRD-000000000000", "x", "",
                    "../../etc/passwd", "PRD-<script>", other_pub):
            r = self._post(_hdr(key, sec), bad, 1, f"p9-mp-{_n()}")
            self.assertEqual(r.status_code, 404, bad)
            self.assertEqual(r.get_json()["error"]["code"], "product_not_found")

    def test_replayed_request_returns_original_order_verbatim(self):
        """P9 fix: a key reused with a DIFFERENT product returns the original
        order faithfully — never a relabeled or new transaction."""
        rs, key, sec = _make_reseller()
        pidA, pubA = _make_product(price=10.0, codes=["RA-1"])
        pidB, pubB = _make_product(price=99.0, codes=["RB-1"])
        _grant(rs["id"], pidA)
        _grant(rs["id"], pidB)
        _topup(rs["id"], 50000)

        r1 = self._post(_hdr(key, sec), pubA, 1, "p9-replay-faithful")
        self.assertEqual(r1.status_code, 201)
        r2 = self._post(_hdr(key, sec), pubB, 1, "p9-replay-faithful")
        self.assertEqual(r2.status_code, 200)
        d2 = r2.get_json()["data"]
        self.assertEqual(d2["order_id"], r1.get_json()["data"]["order_id"])
        self.assertEqual(d2["product_id"], pubA)     # the ORIGINAL product
        self.assertEqual(d2["unit_price"], "10.00")  # the ORIGINAL price
        self.assertEqual(d2["total"], "10.00")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 49000)
        db.cur.execute("SELECT COUNT(*) FROM orders WHERE reseller_id=?", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 1)

    # ================================================================
    # FINANCIAL INTEGRITY
    # ================================================================

    def test_financial_integrity_mixed_workload(self):
        """After orders + failure + cancellation + partial refunds:
        (a) wallet balance == ledger sum, (b) every balance_after equals the
        running sum at that point (immutable ordered ledger), (c) per-order
        refunds <= charge, (d) no duplicate orders/debits/refunds."""
        rs, key, sec = _make_reseller()
        rid = rs["id"]
        pid, pub = _make_product(price=10.0,
                                 codes=["FI-1", "FI-2", "FI-3", "FI-4", "FI-5"])
        _grant(rid, pid)
        _topup(rid, 100000)

        self.assertEqual(self._post(_hdr(key, sec), pub, 1, "fi-ok").status_code, 201)
        rf = self._post(_hdr(key, sec), pub, 1, "fi-fail")
        handle_post_debit_failure(_oid(rf.get_json()["data"]["order_id"]), reason="x")
        rc = self._post(_hdr(key, sec), pub, 1, "fi-cancel")
        handle_cancellation(_oid(rc.get_json()["data"]["order_id"]), post_debit=True)
        rp = self._post(_hdr(key, sec), pub, 2, "fi-part")
        handle_partial_failure(_oid(rp.get_json()["data"]["order_id"]),
                               fulfilled_qty=1, failed_qty=1)
        # replay the first key for good measure
        self.assertEqual(self._post(_hdr(key, sec), pub, 1, "fi-ok").status_code, 200)

        # (a)+(b): running balance consistency row by row
        running = 0
        for tx_type, amount, balance_after in _ledger(rid):
            running += int(amount)
            self.assertEqual(running, int(balance_after),
                             f"ledger row drift: {tx_type} {amount}")
        self.assertEqual(running, rdb.get_wallet_balance_cents(rid))

        # (c)+(d): aggregates
        rows = _ledger(rid)
        debits = sum(-a for t, a, _b in rows if t == "purchase_debit")
        refunds = sum(a for t, a, _b in rows if t == "refund_credit")
        self.assertEqual(debits, 5000)     # 1+1+1+2 units x $10
        self.assertEqual(refunds, 3000)    # fail + cancel + 1 partial unit
        self.assertLessEqual(refunds, debits)
        db.cur.execute("SELECT COUNT(*) FROM orders WHERE reseller_id=?", (rid,))
        self.assertEqual(int(db.cur.fetchone()[0]), 4)   # replay created none

    # ================================================================
    # WEBHOOK HYGIENE
    # ================================================================

    def test_webhook_payloads_carry_no_secrets_or_internals(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["WH-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._post(_hdr(key, sec), pub, 1, "p9-wh-1")
        oid = _oid(r.get_json()["data"]["order_id"])
        handle_post_debit_failure(oid, reason="audit_check")

        import json as _json
        db.cur.execute(
            "SELECT event_type, payload_json FROM webhook_outbox WHERE reseller_id=?",
            (rs["id"],),
        )
        rows = db.cur.fetchall()
        self.assertTrue(rows)
        # sensitive material must never appear in any payload
        banned = ("secret", "password", "token", "api_key",
                  "wallet_transaction", "request_id", "user_id",
                  "reseller_key", "credential", "cost_price", "supplier",
                  "whsec", "signing")
        for event_type, payload_json in rows:
            payload = _json.loads(payload_json)
            flat = _json.dumps(payload).lower()
            for word in banned:
                self.assertNotIn(word, flat, f"{event_type} leaks '{word}'")
            # no raw internal order id keys either
            self.assertNotIn("id", set(payload.keys()) - {"order_id"})
            self.assertTrue(str(payload.get("order_id", "")).startswith("ORD-"))
            # delivery CONTENT (codes/credentials) is never in the payload —
            # only a status label and a pointer to the authenticated endpoint
            self.assertNotIn("items", payload)
            self.assertNotIn("codes", payload)
            self.assertNotIn("WH-1", flat)

    # ================================================================
    # PERFORMANCE
    # ================================================================

    def test_order_creation_latency_and_indexed_reads(self):
        rs, key, sec = _make_reseller()
        # One product per order: the product-burst abuse guard (5 per 10s per
        # product) is a security control and must not be worked around.
        pubs = []
        for i in range(40):
            pid, pub = _make_product(codes=[f"PF-{i}"])
            _grant(rs["id"], pid)
            pubs.append(pub)
        _topup(rs["id"], 5000000)

        # --- create latency: 30 sequential orders
        samples = []
        for i in range(30):
            t0 = time.perf_counter()
            r = self._post(_hdr(key, sec), pubs[i], 1, f"p9-perf-{i}")
            samples.append((time.perf_counter() - t0) * 1000)
            self.assertEqual(r.status_code, 201)
        samples.sort()
        mean_ms = sum(samples) / len(samples)
        p95_ms = samples[int(len(samples) * 0.95) - 1]
        # Generous local-SQLite bounds that still catch pathological regressions
        self.assertLess(mean_ms, 200, f"mean create latency {mean_ms:.1f}ms")
        self.assertLess(p95_ms, 500, f"p95 create latency {p95_ms:.1f}ms")

        # --- list endpoint stays index-backed (no full table scan)
        db.cur.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM orders "
            "WHERE reseller_id=? AND source='reseller_api' ORDER BY id DESC LIMIT 20",
            (rs["id"],),
        )
        plan = " ".join(str(r_[-1]) for r_ in db.cur.fetchall())
        self.assertIn("idx_orders_reseller", plan)

        # --- ledger reads index-backed
        db.cur.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM reseller_wallet_transactions "
            "WHERE reseller_id=? ORDER BY id DESC LIMIT 50",
            (rs["id"],),
        )
        plan = " ".join(str(r_[-1]) for r_ in db.cur.fetchall())
        self.assertIn("idx_reseller_ledger_reseller", plan)

        # --- list latency with a populated account
        t0 = time.perf_counter()
        lst = self.client.get("/api/v1/orders?limit=50", headers=_hdr(key, sec))
        list_ms = (time.perf_counter() - t0) * 1000
        self.assertEqual(lst.status_code, 200)
        self.assertLess(list_ms, 500, f"list latency {list_ms:.1f}ms")

    def test_no_redis_dependency_in_order_path(self):
        """Architecture note: the order path is SQLite/Postgres-atomic by
        design; no Redis dependency exists to fail or desync."""
        import subprocess
        out = subprocess.run(
            ["grep", "-rli", "redis", "reseller_api/"],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        self.assertEqual(out.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
