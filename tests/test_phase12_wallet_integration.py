"""Phase 12 Prompt 4 — wallet/payment integration verification & hardening.

Verifies financial integrity of reseller ordering against the EXISTING wallet
architecture (reseller_wallet + reseller_wallet_transactions ledger +
post_wallet_transaction atomicity). No new wallet/balance/ledger systems.

Covers:
  - sufficient balance (ledger traceability: reseller/order/amount/currency/
    type/timestamp; no internal wallet fields leak into the API response)
  - insufficient balance (402; no debit; stock released)
  - exact balance (debit to exactly zero succeeds)
  - insufficient by one cent (402)
  - concurrent orders against one balance (only one wins; never negative)
  - duplicate order (single debit)
  - order failure -> existing refund mechanism (repaired in P4)
  - cancellation refund + partial refund (repaired tx types)
  - API timeout retry (same client_order_id -> same order, one debit)
  - database failure mid-commit (wallet + order roll back together)
  - worker failure (financial state commits independently of webhooks)
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase12_wallet_", suffix=".db")
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

_ctr = 0


def _n():
    global _ctr
    _ctr += 1
    return _ctr


def _make_reseller(name=None):
    n = _n()
    name = name or f"P12WRes{n}"
    uid = 9_970_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, name, name.lower()),
    )
    db.conn.commit()
    rs = rdb.create_reseller(name, uid, name.lower())
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=(), max_per_order=1):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P12W Product {n}", price),
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P12W-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


def _ledger(rid, tx_type=None):
    sql = ("SELECT tx_type, amount_cents, balance_after_cents, reference,"
           " description, created, currency, related_order_reference"
           " FROM reseller_wallet_transactions WHERE reseller_id=?")
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


def _internal_order_id(public_order_id):
    return int(public_order_id.replace("ORD-", ""))


class Phase12WalletIntegrationTests(unittest.TestCase):
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

    # ------------------------------------------------------- sufficient balance

    def test_sufficient_balance_ledger_traceability_no_leaks(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["W-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)

        r = self._order(key, sec, pub, 1, "w-trace-1")
        self.assertEqual(r.status_code, 201, r.get_json())
        data = r.get_json()["data"]

        # The API response exposes no internal financial fields
        for leaked in ("wallet_transaction_id", "wallet_reference", "balance_cents",
                       "wallet_balance", "reference", "reseller_id", "request_id"):
            self.assertNotIn(leaked, data)

        # Ledger row: traceable to reseller, order (via request_id), amount,
        # currency, type, timestamp
        debits = _ledger(rs["id"], "purchase_debit")
        self.assertEqual(len(debits), 1)
        tx_type, amount, balance_after, reference, desc, created, currency, related = debits[0]
        self.assertEqual(tx_type, "purchase_debit")
        self.assertEqual(amount, -1000)                      # negative = debit
        self.assertEqual(balance_after, 50000 - 1000)        # running balance
        self.assertEqual(currency, "USDT")
        self.assertTrue(created)
        self.assertEqual(related, reference)
        m = re.fullmatch(r"rapi\.(\d+)\.(.+)", reference)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), rs["id"])
        # The reference embeds the server request_id stored on the order row
        db.cur.execute(
            "SELECT request_id, unit_price_cents, quantity FROM orders WHERE reseller_id=?",
            (rs["id"],),
        )
        req_id, unit_cents, qty = db.cur.fetchone()
        self.assertEqual(reference, f"rapi.{rs['id']}.{req_id}")
        self.assertEqual(unit_cents * qty, -amount)

    # ------------------------------------------------------- balance edges

    def test_insufficient_balance_no_debit_stock_released(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["W-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 500)
        r = self._order(key, sec, pub, 1)
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.get_json()["error"]["code"], "insufficient_balance")
        self.assertEqual(_ledger(rs["id"], "purchase_debit"), [])
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 500)
        self.assertEqual(_sold_count(pid), 0)

    def test_exact_balance_succeeds_to_zero(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["W-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 1000)  # exactly the order total
        r = self._order(key, sec, pub, 1)
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 0)

    def test_one_cent_short_402(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["W-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 999)  # one cent short
        r = self._order(key, sec, pub, 1)
        self.assertEqual(r.status_code, 402)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 999)
        self.assertEqual(_sold_count(pid), 0)

    def test_zero_balance_never_negative(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["W-1"])
        _grant_and_import(rs["id"], pid)
        r = self._order(key, sec, pub, 1)  # no topup at all
        self.assertEqual(r.status_code, 402)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 0)

    # ------------------------------------------------------- concurrency

    def test_concurrent_orders_same_balance_only_one_wins(self):
        """Balance $10, two simultaneous $10 orders: exactly one succeeds."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["W-1", "W-2"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 1000)  # 10.00 — enough for ONE order only

        results = []

        def worker(i):
            client = self.app.test_client()
            r = client.post(
                "/api/v1/orders", headers=_hdr(key, sec),
                json={"product_id": pub, "quantity": 1,
                      "client_order_id": f"w-race-bal-{i}"},
            )
            results.append(r.status_code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(results), [201, 402], results)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 0)  # never negative
        self.assertEqual(len(_ledger(rs["id"], "purchase_debit")), 1)
        self.assertEqual(_sold_count(pid), 1)  # loser's reservation was released
        db.cur.execute("SELECT COUNT(*) FROM orders WHERE reseller_id=?", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 1)

    # ------------------------------------------------------- duplicates / retry

    def test_duplicate_order_single_debit(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["W-1", "W-2"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r1 = self._order(key, sec, pub, 1, "w-dup-1")
        r2 = self._order(key, sec, pub, 1, "w-dup-1")
        self.assertEqual((r1.status_code, r2.status_code), (201, 200))
        self.assertEqual(len(_ledger(rs["id"], "purchase_debit")), 1)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000 - 1000)
        self.assertEqual(_sold_count(pid), 1)

    def test_api_timeout_then_retry_same_order(self):
        """Client times out after the server committed; retry returns the
        SAME order with no second debit."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["W-1", "W-2"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r1 = self._order(key, sec, pub, 1, "w-timeout-1")
        self.assertEqual(r1.status_code, 201)
        # ...client times out before reading the response; it retries blindly
        r2 = self._order(key, sec, pub, 1, "w-timeout-1")
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.get_json()["data"]["idempotent"])
        self.assertEqual(r2.get_json()["data"]["order_id"],
                         r1.get_json()["data"]["order_id"])
        self.assertEqual(len(_ledger(rs["id"], "purchase_debit")), 1)
        self.assertEqual(_sold_count(pid), 1)

    # ------------------------------------------------------- failure + refund

    def test_order_failure_uses_existing_refund_mechanism(self):
        """Post-commit failure: handle_post_debit_failure marks the order
        failed, refunds the full amount, restores stock — idempotently."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["W-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._order(key, sec, pub, 1, "w-fail-1")
        self.assertEqual(r.status_code, 201, r.get_json())
        oid = _internal_order_id(r.get_json()["data"]["order_id"])
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 49000)

        from reseller_api.services.order_failure import handle_post_debit_failure
        res1 = handle_post_debit_failure(oid, reason="fulfillment_failure")
        self.assertEqual(res1["result"], "handled")
        self.assertTrue(res1["refunded"])
        self.assertTrue(res1["stock_restored"])

        # Wallet restored via ONE refund_credit ledger row
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)
        refunds = _ledger(rs["id"], "refund_credit")
        self.assertEqual(len(refunds), 1)
        self.assertEqual(refunds[0][1], 1000)  # positive credit
        self.assertEqual(refunds[0][3], f"rapi.refund.order.{oid}")
        # Order marked failed; stock returned
        db.cur.execute("SELECT master_status FROM orders WHERE id=?", (oid,))
        self.assertIn("Failed", db.cur.fetchone()[0])
        self.assertEqual(_sold_count(pid), 0)

        # Idempotent: a second handler run changes nothing
        res2 = handle_post_debit_failure(oid)
        self.assertEqual(res2["result"], "already_terminal")
        self.assertEqual(len(_ledger(rs["id"], "refund_credit")), 1)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)

    def test_cancellation_refund_uses_whitelisted_tx_type(self):
        """P4 repair: cancellation refunds must post as refund_credit with a
        distinct reference (the old cancellation_refund type was never in the
        schema whitelist and would have crashed the ledger write)."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["W-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._order(key, sec, pub, 1, "w-cancel-1")
        self.assertEqual(r.status_code, 201)
        oid = _internal_order_id(r.get_json()["data"]["order_id"])

        from reseller_api.services.order_failure import handle_cancellation
        res = handle_cancellation(oid, post_debit=True)
        self.assertEqual(res["result"], "cancelled")
        self.assertTrue(res["refunded"])

        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)
        refunds = _ledger(rs["id"], "refund_credit")
        self.assertEqual(len(refunds), 1)
        self.assertEqual(refunds[0][3], f"rapi.cancel_refund.order.{oid}")
        db.cur.execute("SELECT master_status FROM orders WHERE id=?", (oid,))
        self.assertIn("Cancelled", db.cur.fetchone()[0])

        # Repeat call is a safe no-op (terminal guard)
        res2 = handle_cancellation(oid, post_debit=True)
        self.assertEqual(res2["result"], "already_terminal")
        self.assertEqual(len(_ledger(rs["id"], "refund_credit")), 1)

    def test_partial_refund_posts_and_is_idempotent(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["W-1", "W-2"], max_per_order=5)
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._order(key, sec, pub, 2, "w-partial-1")
        self.assertEqual(r.status_code, 201, r.get_json())
        oid = _internal_order_id(r.get_json()["data"]["order_id"])
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000 - 2000)

        from reseller_api.services.order_failure import handle_partial_failure
        res = handle_partial_failure(oid, fulfilled_qty=1, failed_qty=1)
        self.assertEqual(res["result"], "partial_refund_issued")
        self.assertEqual(res["refund_cents"], 1000)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000 - 1000)
        refunds = _ledger(rs["id"], "refund_credit")
        self.assertEqual(len(refunds), 1)
        self.assertEqual(refunds[0][3], f"rapi.partial_refund.order.{oid}")
        # Order NOT failed; one unit of stock returned
        db.cur.execute("SELECT master_status FROM orders WHERE id=?", (oid,))
        self.assertNotIn("Failed", str(db.cur.fetchone()[0] or ""))
        self.assertEqual(_sold_count(pid), 1)

        # Replay: UNIQUE(reseller_id, tx_type, reference) dedupes the ledger
        res2 = handle_partial_failure(oid, fulfilled_qty=1, failed_qty=1)
        self.assertEqual(res2["refund_cents"], 1000)
        self.assertEqual(len(_ledger(rs["id"], "refund_credit")), 1)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000 - 1000)

    # ------------------------------------------------------- infrastructure failure

    def test_database_failure_rolls_back_wallet_and_order(self):
        """A failure during the order INSERT must roll back the wallet debit
        in the same atomic block, and release the reserved stock."""
        import reseller_api.services.order_service as osvc

        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["W-1"])
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
            r = self._order(key, sec, pub, 1, "w-dbfail-1")

        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.get_json()["error"]["code"], "order_creation_failed")
        # Wallet NOT debited, no ledger row, no order row, stock released
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)
        self.assertEqual(_ledger(rs["id"], "purchase_debit"), [])
        db.cur.execute("SELECT COUNT(*) FROM orders WHERE reseller_id=?", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 0)
        self.assertEqual(_sold_count(pid), 0)

    def test_worker_failure_cannot_corrupt_financials(self):
        """The order + outbox event commit atomically; the delivery worker is
        a separate concern. If it never runs (or crashes), the financial
        state is already durable and correct."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["W-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._order(key, sec, pub, 1, "w-worker-1")
        self.assertEqual(r.status_code, 201, r.get_json())
        oid = _internal_order_id(r.get_json()["data"]["order_id"])

        # No webhook worker runs in this test process at all.
        self.assertEqual(len(_ledger(rs["id"], "purchase_debit")), 1)
        db.cur.execute(
            "SELECT event_type, status FROM webhook_outbox WHERE aggregate_id=?",
            (f"ORD-{oid:08d}",),
        )
        events = {row[0]: row[1] for row in db.cur.fetchall()}
        self.assertIn("order.created", events)
        # event persisted for later delivery — worker downtime loses nothing
        self.assertEqual(events["order.created"], "pending")


if __name__ == "__main__":
    unittest.main()
