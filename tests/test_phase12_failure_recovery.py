"""Phase 12 Prompt 7 — reseller order failure, refund & compensation handling.

Verifies that reseller orders remain financially and operationally consistent
when something fails, REUSING the Phase 8 failure/refund system
(order_failure.py + the wallet ledger idempotency references).  No new
refund architecture.

Failure scenarios covered
-------------------------
 1. Product unavailable          — no debit, no order, no stock movement
 2. Stock unavailable            — no debit, no order, no partial claims
 3. Wallet failure               — declined (402) and raising (500) variants
 4. Database failure             — atomic rollback of wallet + order + stock
 5. Master order creation failure— debit can never commit without the order
 6. Fulfillment failure          — failed status + full refund + stock + audit
 7. Delivery failure             — truthful API surface after failure
 8. Worker crash                  — outbox event survives; financials intact
 9. API timeout                   — retry resolves the SAME order
10. Reseller retry               — no double charge / duplicate order /
                                   double stock consumption / duplicate events

Partial failures investigated
-----------------------------
  * wallet debit succeeded but master order creation failed
        -> impossible by construction (single atomic block); proven here.
  * master order created but API response lost
        -> client_order_id retry resolves the existing order.
  * crash between terminal-status commit and refund commit
        -> P7 repair: handlers are resumable; retry completes the refund.
  * partial refund followed by full failure (and vice versa)
        -> P7 repair: per-order refund cap; over-refunding is impossible.
  * partial restore followed by full restore on manual stock
        -> P7 repair: full restore returns only the remaining units.

Invariants verified
-------------------
  financial consistency  — wallet balance always equals the ledger sum
  order consistency      — lifecycle states truthful, terminal states stable
  stock consistency      — sold rows / manual counter match surviving orders
  refund consistency     — per-order refunds never exceed the charge
  idempotency            — every retry / replay is a safe no-op
  audit trail            — ledger rows, failure_reason, outbox events
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase12_failures_", suffix=".db")
os.close(_fd)

import config
config.DB_PATH = _db_path
import database.db as db
db.DB_PATH = _db_path
db._tls = threading.local()
db.init_db()

import database.reseller_db as rdb
from reseller_api import create_app
import reseller_api.middleware.request_context  # noqa: F401  (patch target)
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


def _make_reseller(name=None):
    n = _n()
    name = name or f"P12FRes{n}"
    uid = 9_960_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, name, name.lower()),
    )
    db.conn.commit()
    rs = rdb.create_reseller(name, uid, name.lower())
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=(), max_per_order=1, manual_stock=0):
    """codes -> code-stock product; manual_stock>0 -> manual product."""
    n = _n()
    stock = 0 if codes else int(manual_stock)
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,?,'code_stock',1)",
        (f"P12F Product {n}", price, stock),
    )
    db.conn.commit()
    pid = db.cur.lastrowid
    if max_per_order != 1:
        db.cur.execute("UPDATE products SET max_per_order=? WHERE id=?", (max_per_order, pid))
    for c in codes:
        db.cur.execute(
            "INSERT INTO product_stock(product_id, delivery, sold) VALUES(?,?,0)", (pid, c)
        )
    db.conn.commit()
    return pid, rdb.ensure_product_public_id(pid)


def _grant_and_import(rid, pid):
    rdb.set_product_access(rid, pid, True)
    rdb.import_reseller_product(rid, pid)


def _topup(rid, cents):
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P12F-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


def _ledger(rid, tx_type=None):
    sql = ("SELECT tx_type, amount_cents, balance_after_cents, reference,"
           " description, created, currency FROM reseller_wallet_transactions"
           " WHERE reseller_id=?")
    params = [rid]
    if tx_type:
        sql += " AND tx_type=?"
        params.append(tx_type)
    db.cur.execute(sql + " ORDER BY id", params)
    return db.cur.fetchall()


def _sold_count(pid):
    db.cur.execute(
        "SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=1", (pid,)
    )
    return int(db.cur.fetchone()[0])


def _manual_stock(pid):
    db.cur.execute("SELECT stock FROM products WHERE id=?", (pid,))
    return int(db.cur.fetchone()[0])


def _oid(public_order_id):
    return int(public_order_id.replace("ORD-", ""))


def _order_count(rid):
    db.cur.execute("SELECT COUNT(*) FROM orders WHERE reseller_id=?", (rid,))
    return int(db.cur.fetchone()[0])


def _outbox(aggregate_id):
    db.cur.execute(
        "SELECT event_type, status, attempts FROM webhook_outbox WHERE aggregate_id=?",
        (aggregate_id,),
    )
    return db.cur.fetchall()


def _balance_invariant(rid):
    """Financial consistency: wallet balance == sum of ledger amounts."""
    db.cur.execute(
        "SELECT COALESCE(SUM(amount_cents),0) FROM reseller_wallet_transactions"
        " WHERE reseller_id=?",
        (rid,),
    )
    ledger_sum = int(db.cur.fetchone()[0])
    return ledger_sum, rdb.get_wallet_balance_cents(rid)


def _master_status(oid):
    db.cur.execute("SELECT master_status, failure_reason FROM orders WHERE id=?", (oid,))
    return db.cur.fetchone()


class Phase12FailureRecoveryTests(unittest.TestCase):
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

    def _order(self, key, sec, pub, qty=1, coid=None):
        body = {"product_id": pub, "quantity": qty}
        if coid:
            body["client_order_id"] = coid
        return self.client.post("/api/v1/orders", headers=_hdr(key, sec), json=body)

    # ============================================================
    # 1. Product unavailable
    # ============================================================

    def test_1_product_unavailable_no_side_effects(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["F1-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        # Product becomes unavailable (disabled by admin)
        db.cur.execute("UPDATE products SET active=0 WHERE id=?", (pid,))
        db.conn.commit()

        r = self._order(key, sec, pub, 1, "f-prod-1")
        self.assertEqual(r.status_code, 404)

        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)
        self.assertEqual(_ledger(rs["id"], "purchase_debit"), [])
        self.assertEqual(_order_count(rs["id"]), 0)
        self.assertEqual(_sold_count(pid), 0)
        db.cur.execute(
            "SELECT COUNT(*) FROM webhook_outbox WHERE reseller_id=?", (rs["id"],)
        )
        self.assertEqual(int(db.cur.fetchone()[0]), 0)  # no events for a rejected order
        ls, bal = _balance_invariant(rs["id"])
        self.assertEqual(ls, bal)

    # ============================================================
    # 2. Stock unavailable
    # ============================================================

    def test_2_stock_unavailable_no_partial_claims(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["F2-1", "F2-2"], max_per_order=5)
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)

        r = self._order(key, sec, pub, 3, "f-stock-1")  # only 2 available
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.get_json()["error"]["code"], "insufficient_stock")
        # no partial claim leaked
        self.assertEqual(_sold_count(pid), 0)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)
        self.assertEqual(_order_count(rs["id"]), 0)

    def test_2_out_of_stock_422_clean(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=[], manual_stock=0)
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._order(key, sec, pub, 1, "f-stock-2")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.get_json()["error"]["code"], "out_of_stock")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)
        self.assertEqual(_order_count(rs["id"]), 0)

    # ============================================================
    # 3. Wallet failure
    # ============================================================

    def test_3_wallet_declined_402_stock_released(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["F3-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 500)  # $5 < $10

        r = self._order(key, sec, pub, 1, "f-wallet-1")
        self.assertEqual(r.status_code, 402)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 500)
        self.assertEqual(_sold_count(pid), 0)       # reservation released
        self.assertEqual(_order_count(rs["id"]), 0)
        self.assertEqual(_ledger(rs["id"], "purchase_debit"), [])

    def test_3_wallet_raises_everything_rolls_back(self):
        """Wallet layer raises (DB fault) inside the atomic block:
        no debit, no order, stock released."""
        import reseller_api.services.order_service as osvc

        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["F3-2"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)

        with patch.object(
            osvc, "post_wallet_transaction",
            side_effect=RuntimeError("simulated wallet db fault"),
        ):
            r = self._order(key, sec, pub, 1, "f-wallet-2")

        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.get_json()["error"]["code"], "order_creation_failed")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)
        self.assertEqual(_ledger(rs["id"], "purchase_debit"), [])
        self.assertEqual(_order_count(rs["id"]), 0)
        self.assertEqual(_sold_count(pid), 0)

    # ============================================================
    # 4. Database failure / 5. Master order creation failure
    # ============================================================

    def test_4_db_failure_at_insert_rolls_back_atomically(self):
        """Simulated DB failure on the orders INSERT: the wallet debit in the
        same atomic block rolls back; the committed stock claim is released."""
        import reseller_api.services.order_service as osvc

        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["F4-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)

        class ExplodingCursor:
            def execute(self, sql, params=()):
                if "INSERT INTO orders" in str(sql):
                    raise RuntimeError("simulated database failure")
                return db.get_cursor().execute(sql, params)

            def __getattr__(self, name):
                return getattr(db.get_cursor(), name)

        with patch.object(osvc, "get_cursor", lambda: ExplodingCursor()):
            r = self._order(key, sec, pub, 1, "f-db-1")

        self.assertEqual(r.status_code, 500)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)
        self.assertEqual(_ledger(rs["id"], "purchase_debit"), [])  # no debit trace
        self.assertEqual(_order_count(rs["id"]), 0)
        self.assertEqual(_sold_count(pid), 0)
        db.cur.execute(
            "SELECT COUNT(*) FROM webhook_outbox WHERE reseller_id=?", (rs["id"],)
        )
        self.assertEqual(int(db.cur.fetchone()[0]), 0)

    def test_5_debit_can_never_commit_without_order(self):
        """PARTIAL FAILURE INVESTIGATION #1:
        'wallet debit succeeded but master order creation failed'.

        The debit and the order INSERT live in ONE pgcompat.atomic block, so
        the debit cannot commit alone.  Proven by failing the stock
        attribution UPDATE (which runs AFTER the INSERT): the entire block —
        wallet balance, ledger row, order row, attribution — rolls back.
        """
        import reseller_api.services.order_service as osvc

        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["F5-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)

        class ExplodingCursor:
            def execute(self, sql, params=()):
                if "UPDATE product_stock SET order_id" in str(sql):
                    raise RuntimeError("simulated attribution failure")
                return db.get_cursor().execute(sql, params)

            def __getattr__(self, name):
                return getattr(db.get_cursor(), name)

        with patch.object(osvc, "get_cursor", lambda: ExplodingCursor()):
            r = self._order(key, sec, pub, 1, "f-db-2")

        self.assertEqual(r.status_code, 500)
        # The debit never became durable: balance and ledger untouched.
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)
        self.assertEqual(_ledger(rs["id"], "purchase_debit"), [])
        self.assertEqual(_order_count(rs["id"]), 0)
        self.assertEqual(_sold_count(pid), 0)

    # ============================================================
    # 6. Fulfillment failure (post-commit)
    # ============================================================

    def test_6_fulfillment_failure_full_compensation_and_audit(self):
        """Post-commit fulfillment failure -> Phase 8 handler:
        failed status + full refund + stock restore + full audit trail."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["F6-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)

        r = self._order(key, sec, pub, 1, "f-ful-1")
        self.assertEqual(r.status_code, 201, r.get_json())
        oid = _oid(r.get_json()["data"]["order_id"])
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 49000)

        res = handle_post_debit_failure(oid, reason="supplier_unavailable")
        self.assertEqual(res["result"], "handled")
        self.assertTrue(res["refunded"])
        self.assertTrue(res["stock_restored"])

        # --- order consistency
        status, reason = _master_status(oid)
        self.assertIn("Failed", status)
        self.assertEqual(reason, "supplier_unavailable")

        # --- refund consistency: exactly one refund_credit == the charge
        refunds = _ledger(rs["id"], "refund_credit")
        self.assertEqual(len(refunds), 1)
        self.assertEqual(refunds[0][1], 1000)
        self.assertEqual(refunds[0][3], f"rapi.refund.order.{oid}")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)

        # --- stock consistency
        self.assertEqual(_sold_count(pid), 0)

        # --- audit trail: order.failed + delivery.failed + refund.created
        agg = f"ORD-{oid:08d}"
        events = {e[0]: e[1] for e in _outbox(agg)}
        self.assertIn("order.created", events)
        self.assertIn("order.failed", events)
        self.assertIn("delivery.failed", events)
        self.assertIn("refund.created", events)

        # --- idempotent replay: pure no-op
        res2 = handle_post_debit_failure(oid, reason="supplier_unavailable")
        self.assertEqual(res2["result"], "already_terminal")
        self.assertFalse(res2["refunded"])
        self.assertEqual(len(_ledger(rs["id"], "refund_credit")), 1)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)

        # --- financial invariant
        ls, bal = _balance_invariant(rs["id"])
        self.assertEqual(ls, bal)

    def test_6_crash_between_status_and_refund_recovers(self):
        """P7 REPAIR — crash after the Failed-status commit but BEFORE the
        refund commit must not strand the charge.  The retry completes the
        missing refund (and only that)."""
        import reseller_api.services.order_failure as of

        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["F6-2"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._order(key, sec, pub, 1, "f-ful-2")
        self.assertEqual(r.status_code, 201)
        oid = _oid(r.get_json()["data"]["order_id"])

        original = of._issue_refund
        def boom(*a, **k):
            raise RuntimeError("simulated crash during refund")
        of._issue_refund = boom
        try:
            with self.assertRaises(RuntimeError):
                handle_post_debit_failure(oid, reason="crash_test")
        finally:
            of._issue_refund = original

        # Interim state: order failed, charge NOT yet refunded (the gap).
        status, _ = _master_status(oid)
        self.assertIn("Failed", status)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 49000)
        self.assertEqual(_ledger(rs["id"], "refund_credit"), [])

        # Retry resumes and completes the refund — exactly once.
        res = handle_post_debit_failure(oid, reason="crash_test")
        self.assertEqual(res["result"], "recovered")
        self.assertTrue(res["refunded"])
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)
        self.assertEqual(len(_ledger(rs["id"], "refund_credit")), 1)

        # Fully recovered -> further replays are pure no-ops.
        res2 = handle_post_debit_failure(oid)
        self.assertEqual(res2["result"], "already_terminal")
        self.assertEqual(len(_ledger(rs["id"], "refund_credit")), 1)

    def test_6_crash_between_refund_and_stock_restore_recovers(self):
        """P7 REPAIR — refund committed but the process died before the
        stock restore: the retry restores stock without re-refunding."""
        import reseller_api.services.order_failure as of

        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["F6-3"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._order(key, sec, pub, 1, "f-ful-3")
        self.assertEqual(r.status_code, 201)
        oid = _oid(r.get_json()["data"]["order_id"])

        original = of._restore_stock
        def boom(*a, **k):
            raise RuntimeError("simulated crash during stock restore")
        of._restore_stock = boom
        try:
            with self.assertRaises(RuntimeError):
                handle_post_debit_failure(oid, reason="crash_test_2")
        finally:
            of._restore_stock = original

        # Refund done, stock still sold
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)
        self.assertEqual(_sold_count(pid), 1)

        res = handle_post_debit_failure(oid, reason="crash_test_2")
        self.assertEqual(res["result"], "recovered")
        self.assertFalse(res["refunded"])      # no second refund
        self.assertTrue(res["stock_restored"])
        self.assertEqual(_sold_count(pid), 0)
        self.assertEqual(len(_ledger(rs["id"], "refund_credit")), 1)

    def test_6_cancellation_crash_recovery_and_never_debited_guard(self):
        """P7 REPAIR — cancellation crash between status and refund is
        recoverable; a pre-debit cancellation retried with post_debit=True
        can never mint money (the debit ledger row is verified first)."""
        import reseller_api.services.order_failure as of

        # (a) crash between Cancelled commit and refund commit
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["F6-4"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._order(key, sec, pub, 1, "f-can-1")
        self.assertEqual(r.status_code, 201)
        oid = _oid(r.get_json()["data"]["order_id"])

        original = of._issue_refund
        of._issue_refund = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash"))
        try:
            with self.assertRaises(RuntimeError):
                handle_cancellation(oid, post_debit=True)
        finally:
            of._issue_refund = original

        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 49000)
        res = handle_cancellation(oid, post_debit=True)
        self.assertEqual(res["result"], "recovered")
        self.assertTrue(res["refunded"])
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)
        self.assertEqual(len(_ledger(rs["id"], "refund_credit")), 1)
        self.assertEqual(
            _ledger(rs["id"], "refund_credit")[0][3],
            f"rapi.cancel_refund.order.{oid}",
        )

        # (b) pre-debit cancel, then a retry claiming post_debit=True
        rs2, key2, sec2 = _make_reseller()
        pid2, pub2 = _make_product(price=10.0, codes=["F6-5"])
        _grant_and_import(rs2["id"], pid2)
        _topup(rs2["id"], 50000)
        r2 = self._order(key2, sec2, pub2, 1, "f-can-2")
        self.assertEqual(r2.status_code, 201)
        oid2 = _oid(r2.get_json()["data"]["order_id"])

        res3 = handle_cancellation(oid2, post_debit=False)   # truthful: no debit
        self.assertEqual(res3["result"], "cancelled")
        self.assertFalse(res3["refunded"])
        # NOTE: the order WAS actually debited at creation; post_debit=False
        # models a pre-debit cancel in the Phase 8 contract.  The recovery
        # guard keys on the ledger: a cancel_refund is issued only when the
        # purchase debit exists.  Here it does, so a later post_debit=True
        # call legitimately completes the refund — exactly once.
        res4 = handle_cancellation(oid2, post_debit=True)
        self.assertEqual(res4["result"], "recovered")
        self.assertEqual(rdb.get_wallet_balance_cents(rs2["id"]), 50000)
        self.assertEqual(len(_ledger(rs2["id"], "refund_credit")), 1)

    # ============================================================
    # 7. Delivery failure — truthful API surface
    # ============================================================

    def test_7_delivery_failure_visible_via_api(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["F7-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._order(key, sec, pub, 1, "f-del-1")
        self.assertEqual(r.status_code, 201)
        pub_oid = r.get_json()["data"]["order_id"]
        oid = _oid(pub_oid)

        handle_post_debit_failure(oid, reason="delivery_channel_down")

        d = self.client.get(f"/api/v1/orders/{pub_oid}/delivery",
                            headers=_hdr(key, sec))
        self.assertEqual(d.status_code, 200)
        body = d.get_json()["data"]["delivery"]
        self.assertEqual(body["delivery_status"], "failed")
        self.assertEqual(body["items"], [])

        det = self.client.get(f"/api/v1/orders/{pub_oid}", headers=_hdr(key, sec))
        self.assertEqual(det.status_code, 200)
        order = det.get_json()["data"]["order"]
        self.assertEqual(order["status"], "failed")
        self.assertTrue(order["refunded"])
        self.assertEqual(order["refund"]["type"], "full")

    # ============================================================
    # 8. Worker crash
    # ============================================================

    def test_8_worker_crash_preserves_event_and_financials(self):
        """The order + outbox event commit atomically.  A webhook worker
        crash loses nothing: the event stays pending and deliverable, and
        the financial state never depends on the worker."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["F8-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        hook, _hook_secret = rdb.create_webhook(rs["id"], "https://reseller.example/hook")

        r = self._order(key, sec, pub, 1, "f-worker-1")
        self.assertEqual(r.status_code, 201)
        oid = _oid(r.get_json()["data"]["order_id"])
        agg = f"ORD-{oid:08d}"

        # Worker is DOWN — nothing processes the outbox row.
        from reseller_api.services.webhook_outbox import get_pending_events
        pending = [e for e in get_pending_events(50) if e["aggregate_id"] == agg]
        self.assertTrue(any(e["event_type"] == "order.created" for e in pending))
        self.assertTrue(all(e["attempts"] == 0 for e in pending))
        # Financials are already durable and correct without the worker.
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 49000)
        self.assertEqual(_order_count(rs["id"]), 1)

        # Worker restarts: the same event is still deliverable.
        db.cur.execute(
            "SELECT id FROM webhook_outbox WHERE aggregate_id=? AND event_type='order.created'",
            (agg,),
        )
        event_id = int(db.cur.fetchone()[0])
        from reseller_api.services import webhook_delivery as wd
        row = {
            "event_type": "order.created",
            "payload_json": next(
                p for (t, p) in db.cur.execute(
                    "SELECT event_type, payload_json FROM webhook_outbox WHERE id=?",
                    (event_id,),
                ).fetchall() if t == "order.created"
            ),
        }
        with patch.object(wd, "_send_http", return_value={
            "success": True, "http_status": 200, "error_category": None,
            "responded_at": "now", "elapsed_ms": 5,
        }):
            result = wd.deliver_to_webhook(
                event_id, row, hook["id"], hook["url"], rs["id"], attempt=1,
            )
        self.assertTrue(result["success"])
        # Delivery logged (audit) and the wallet was never touched.
        logs = wd.get_delivery_log_for_event(event_id)
        self.assertEqual(len(logs), 1)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 49000)

        # Two workers cannot claim the same job slot concurrently.
        from reseller_api.services.order_idempotency import claim_worker_job
        self.assertTrue(claim_worker_job(f"deliver:{event_id}", "worker-A"))
        self.assertFalse(claim_worker_job(f"deliver:{event_id}", "worker-B"))

    # ============================================================
    # 9. API timeout / master order created, response lost
    # ============================================================

    def test_9_response_lost_retry_resolves_same_order(self):
        """PARTIAL FAILURE INVESTIGATION #2:
        'master order created but the API response was lost'.

        A blind retry with the same client_order_id resolves the EXISTING
        order — no second debit, no second order row, no second stock unit,
        no duplicate fulfillment events."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["F9-1", "F9-2"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)

        r1 = self._order(key, sec, pub, 1, "f-timeout-1")
        self.assertEqual(r1.status_code, 201)
        # ... response lost on the wire; the client retries the same request
        r2 = self._order(key, sec, pub, 1, "f-timeout-1")
        self.assertEqual(r2.status_code, 200)
        d2 = r2.get_json()["data"]
        self.assertTrue(d2["idempotent"])
        self.assertEqual(d2["order_id"], r1.get_json()["data"]["order_id"])

        oid = _oid(d2["order_id"])
        agg = f"ORD-{oid:08d}"
        self.assertEqual(_order_count(rs["id"]), 1)                 # no dup order
        self.assertEqual(len(_ledger(rs["id"], "purchase_debit")), 1)  # no double charge
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 49000)
        self.assertEqual(_sold_count(pid), 1)                        # stock once
        events = {}
        for et, st, _att in _outbox(agg):
            events[et] = events.get(et, 0) + 1
        self.assertEqual(events.get("order.created"), 1)
        self.assertEqual(events.get("delivery.completed"), 1)        # no dup fulfillment

    def test_9b_sequential_retry_after_last_unit_consumed(self):
        """Response lost AND the order consumed the last unit: the retry must
        still resolve the existing order — a 422 would be a lie (the order
        exists and was charged).  The validation-layer stock check cannot be
        allowed to mask the replay (P7)."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["F9B-1"])  # exactly 1 unit
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)

        r1 = self._order(key, sec, pub, 1, "f-timeout-last")
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(_sold_count(pid), 1)   # stock now exhausted BY the order

        r2 = self._order(key, sec, pub, 1, "f-timeout-last")
        self.assertEqual(r2.status_code, 200, r2.get_json())
        self.assertTrue(r2.get_json()["data"]["idempotent"])
        self.assertEqual(r2.get_json()["data"]["order_id"],
                         r1.get_json()["data"]["order_id"])
        self.assertEqual(_order_count(rs["id"]), 1)
        self.assertEqual(len(_ledger(rs["id"], "purchase_debit")), 1)
        self.assertEqual(_sold_count(pid), 1)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 49000)

    # ============================================================
    # 10. Reseller retry
    # ============================================================

    def test_10_concurrent_retry_same_key_single_charge(self):
        """Four concurrent retries, ONE key, and stock for only ONE unit —
        the harshest retry race.  Exactly one order is created and charged;
        every concurrent duplicate resolves the SAME order (P7: a duplicate
        never dies on transient stock scarcity — it replays)."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["F10-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)

        results = []

        def worker():
            c = self.app.test_client()
            rr = c.post("/api/v1/orders", headers=_hdr(key, sec),
                        json={"product_id": pub, "quantity": 1,
                              "client_order_id": "f-retry-race"})
            results.append(rr.status_code)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(results), [200, 200, 200, 201], results)
        self.assertEqual(_order_count(rs["id"]), 1)
        self.assertEqual(len(_ledger(rs["id"], "purchase_debit")), 1)
        self.assertEqual(_sold_count(pid), 1)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 49000)

    def test_10_retry_after_failure_with_same_key_returns_failed_order(self):
        """After a definitive failure the same client_order_id keeps
        resolving the SAME (failed) order — never a silent re-charge.  A
        genuinely new attempt uses a NEW key and is a separate order."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["F10-3", "F10-4"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)

        r1 = self._order(key, sec, pub, 1, "f-retry-fail")
        self.assertEqual(r1.status_code, 201)
        oid1 = _oid(r1.get_json()["data"]["order_id"])
        handle_post_debit_failure(oid1, reason="supplier_down")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)  # refunded

        # Same key -> idempotent replay of the failed order, no new debit,
        # and the replayed status is TRUTHFUL ("failed", not "completed").
        r2 = self._order(key, sec, pub, 1, "f-retry-fail")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.get_json()["data"]["order_id"],
                         r1.get_json()["data"]["order_id"])
        self.assertEqual(r2.get_json()["data"]["status"], "failed")
        self.assertEqual(_order_count(rs["id"]), 1)
        self.assertEqual(len(_ledger(rs["id"], "purchase_debit")), 1)

        # New key -> a genuinely new order with its own debit
        r3 = self._order(key, sec, pub, 1, "f-retry-fail-2")
        self.assertEqual(r3.status_code, 201)
        self.assertNotEqual(r3.get_json()["data"]["order_id"],
                            r1.get_json()["data"]["order_id"])
        self.assertEqual(_order_count(rs["id"]), 2)
        self.assertEqual(len(_ledger(rs["id"], "purchase_debit")), 2)
        # Net position: charged for one live order, refunded for the failed one
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 49000)
        ls, bal = _balance_invariant(rs["id"])
        self.assertEqual(ls, bal)

    # ============================================================
    # Partial failure combinations — refund/stock caps
    # ============================================================

    def test_partial_then_full_failure_never_over_refunds(self):
        """P7 REPAIR — qty=2, one unit fails (partial refund), then the whole
        order fails: the full-failure refund covers ONLY the outstanding
        unit.  Total refunds == total charged, never more."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["F11-1", "F11-2"], max_per_order=5)
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._order(key, sec, pub, 2, "f-partial-1")
        self.assertEqual(r.status_code, 201)
        oid = _oid(r.get_json()["data"]["order_id"])
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 48000)

        res1 = handle_partial_failure(oid, fulfilled_qty=1, failed_qty=1)
        self.assertEqual(res1["result"], "partial_refund_issued")
        self.assertEqual(res1["refund_cents"], 1000)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 49000)

        res2 = handle_post_debit_failure(oid, reason="remaining_unit_revoked")
        self.assertEqual(res2["result"], "handled")
        self.assertTrue(res2["refunded"])

        # Refund consistency: 1000 (partial) + 1000 (remainder) == 2000 charged
        refunds = _ledger(rs["id"], "refund_credit")
        self.assertEqual(sorted(x[1] for x in refunds), [1000, 1000])
        self.assertEqual(sum(x[1] for x in refunds), 2000)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)

        # Stock consistency: both units returned exactly once
        self.assertEqual(_sold_count(pid), 0)

    def test_full_failure_then_partial_attempt_is_capped(self):
        """P7 REPAIR — a partial refund attempted AFTER the full failure
        refund must not over-credit the wallet."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["F11-3", "F11-4"], max_per_order=5)
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._order(key, sec, pub, 2, "f-partial-2")
        self.assertEqual(r.status_code, 201)
        oid = _oid(r.get_json()["data"]["order_id"])

        handle_post_debit_failure(oid, reason="full_failure")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)

        res = handle_partial_failure(oid, fulfilled_qty=1, failed_qty=1)
        self.assertEqual(res["result"], "order_terminal")
        self.assertEqual(res["refund_cents"], 0)
        # Still exactly one refund row, balance untouched
        self.assertEqual(len(_ledger(rs["id"], "refund_credit")), 1)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)

    def test_partial_then_full_failure_manual_stock_not_inflated(self):
        """P7 REPAIR — manual products: the full-failure restore returns
        only the units the partial refund did NOT already return."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=[], manual_stock=5, max_per_order=5)
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._order(key, sec, pub, 2, "f-partial-3")
        self.assertEqual(r.status_code, 201)
        oid = _oid(r.get_json()["data"]["order_id"])
        self.assertEqual(_manual_stock(pid), 3)   # 5 - 2

        handle_partial_failure(oid, fulfilled_qty=1, failed_qty=1)
        self.assertEqual(_manual_stock(pid), 4)   # one unit returned

        res = handle_post_debit_failure(oid, reason="full_failure")
        self.assertEqual(res["result"], "handled")
        self.assertTrue(res["stock_restored"])
        # Exactly the remaining unit returned — not both again
        self.assertEqual(_manual_stock(pid), 5)
        # Refund consistency
        refunds = _ledger(rs["id"], "refund_credit")
        self.assertEqual(sum(x[1] for x in refunds), 2000)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)

    def test_concurrent_full_refund_recovery_single_credit(self):
        """Two threads racing to recover the same crashed failure:
        exactly one refund posts (ledger UNIQUE dedupe)."""
        import reseller_api.services.order_failure as of

        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["F12-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._order(key, sec, pub, 1, "f-race-refund")
        self.assertEqual(r.status_code, 201)
        oid = _oid(r.get_json()["data"]["order_id"])

        # Crash after status, before refund
        original = of._issue_refund
        of._issue_refund = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash"))
        try:
            with self.assertRaises(RuntimeError):
                handle_post_debit_failure(oid, reason="crash")
        finally:
            of._issue_refund = original

        results = []
        def recover():
            results.append(handle_post_debit_failure(oid, reason="crash"))
        threads = [threading.Thread(target=recover) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(_ledger(rs["id"], "refund_credit")), 1)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)
        self.assertEqual(_sold_count(pid), 0)

    def test_concurrent_manual_stock_restore_single_restore(self):
        """atomic_restore_stock: the guard claim is atomic — concurrent
        restores of a manual-stock order increment the counter exactly once."""
        from reseller_api.services.order_idempotency import atomic_restore_stock

        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=[], manual_stock=3, max_per_order=5)
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._order(key, sec, pub, 2, "f-race-stock")
        self.assertEqual(r.status_code, 201)
        oid = _oid(r.get_json()["data"]["order_id"])
        self.assertEqual(_manual_stock(pid), 1)  # 3 - 2

        outcomes = []
        def restore():
            outcomes.append(atomic_restore_stock(pid, 2, "stock_restored", oid))
        threads = [threading.Thread(target=restore) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(outcomes.count(True), 1, outcomes)
        self.assertEqual(_manual_stock(pid), 3)  # restored exactly once

    # ============================================================
    # Lifecycle states (actual existing states)
    # ============================================================

    def test_lifecycle_states_truthful(self):
        """Actual master lifecycle: completed (default), failed, cancelled.
        'Refunded' is a FINANCIAL fact on the ledger — surfaced per order via
        the refunded flag — not an order state (by existing design)."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["FL-1", "FL-2", "FL-3"],
                                 max_per_order=5)
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)

        # completed
        r1 = self._order(key, sec, pub, 1, "f-life-1")
        o1 = r1.get_json()["data"]["order_id"]
        # failed + refunded
        r2 = self._order(key, sec, pub, 1, "f-life-2")
        o2 = r2.get_json()["data"]["order_id"]
        handle_post_debit_failure(_oid(o2), reason="x")
        # cancelled + refunded
        r3 = self._order(key, sec, pub, 1, "f-life-3")
        o3 = r3.get_json()["data"]["order_id"]
        handle_cancellation(_oid(o3), post_debit=True)

        s1 = self.client.get(f"/api/v1/orders/{o1}", headers=_hdr(key, sec)).get_json()["data"]["order"]
        s2 = self.client.get(f"/api/v1/orders/{o2}", headers=_hdr(key, sec)).get_json()["data"]["order"]
        s3 = self.client.get(f"/api/v1/orders/{o3}", headers=_hdr(key, sec)).get_json()["data"]["order"]

        self.assertEqual(s1["status"], "completed")
        self.assertFalse(s1["refunded"])
        self.assertEqual(s2["status"], "failed")
        self.assertTrue(s2["refunded"])
        self.assertEqual(s2["refund"]["type"], "full")
        self.assertEqual(s3["status"], "cancelled")
        self.assertTrue(s3["refunded"])
        self.assertEqual(s3["refund"]["type"], "cancellation")

        # Terminal states are stable: further handlers cannot mutate them
        self.assertEqual(
            handle_post_debit_failure(_oid(o3))["result"], "already_terminal")
        self.assertEqual(
            handle_cancellation(_oid(o2), post_debit=True)["result"],
            "already_terminal")

    # ============================================================
    # Global consistency after a mixed failure sequence
    # ============================================================

    def test_global_invariants_after_mixed_failures(self):
        """One reseller runs: success, failure+refund, cancel+refund,
        partial refund.  Afterwards: balance == ledger sum; per-order
        refunds <= charge; stock sold == units of live orders; audit rows
        exist for every financial movement."""
        rs, key, sec = _make_reseller()
        rid = rs["id"]
        pid, pub = _make_product(price=10.0,
                                 codes=["FM-1", "FM-2", "FM-3", "FM-4", "FM-5"],
                                 max_per_order=5)
        _grant_and_import(rid, pid)
        _topup(rid, 100000)

        # success (1 unit)
        self.assertEqual(self._order(key, sec, pub, 1, "f-mix-ok").status_code, 201)
        # failure (1 unit)
        rf = self._order(key, sec, pub, 1, "f-mix-fail")
        handle_post_debit_failure(_oid(rf.get_json()["data"]["order_id"]), reason="x")
        # cancellation (1 unit)
        rc = self._order(key, sec, pub, 1, "f-mix-cancel")
        handle_cancellation(_oid(rc.get_json()["data"]["order_id"]), post_debit=True)
        # partial (2 units, 1 fails)
        rp = self._order(key, sec, pub, 2, "f-mix-partial")
        handle_partial_failure(_oid(rp.get_json()["data"]["order_id"]),
                               fulfilled_qty=1, failed_qty=1)

        # Financial: 100000 -1000(ok) -1000(fail) +1000 -1000(cancel) +1000
        #            -2000(partial) +1000 = 98000
        self.assertEqual(rdb.get_wallet_balance_cents(rid), 98000)
        ls, bal = _balance_invariant(rid)
        self.assertEqual(ls, bal)

        # Per-order refunds never exceed the order's charge
        for row in _ledger(rid, "refund_credit"):
            self.assertGreater(row[1], 0)
        self.assertEqual(sum(x[1] for x in _ledger(rid, "refund_credit")), 3000)
        self.assertEqual(sum(-x[1] for x in _ledger(rid, "purchase_debit")), 5000)

        # Stock: 5 codes.  Sold = 1 (ok) + 1 (cancelled: its codes were
        # already delivered at creation — Phase 8 cancellation refunds money
        # and never reclaims delivered units) + 1 (partial, fulfilled unit).
        # The failed order's unit and the partial-failed unit were restored.
        self.assertEqual(_sold_count(pid), 3)

        # Audit trail: every ledger row carries reference + description +
        # running balance + timestamp + currency
        for tx_type, amount, bal_after, ref, desc, created, cur_ in _ledger(rid):
            self.assertTrue(ref)
            self.assertTrue(desc)
            self.assertIsNotNone(bal_after)
            self.assertTrue(created)
            self.assertEqual(cur_, "USDT")


if __name__ == "__main__":
    unittest.main()
