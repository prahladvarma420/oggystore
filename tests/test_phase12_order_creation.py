"""Phase 12 Prompt 2 — HTTP regression tests for POST /api/v1/orders.

Covers the reseller order creation layer end-to-end through the REAL
middleware stack, real DB, and real services (only rate limiting, audit
persistence, and the global kill-switch are stubbed):

  1.  valid order                 -> 201, wallet debited, stock consumed, linked row
  2.  invalid product             -> 404 product_not_found (bad format / nonexistent)
  3.  product not mapped          -> 404 (owner access grant alone is NOT enough)
  4.  disabled product            -> 404 (active=0)
  5.  mapping disabled            -> 404 (reseller toggled enabled=0 in my-catalog)
  6.  inactive reseller           -> 403 (suspended)
  7.  invalid quantity            -> 400 (0 / negative / fractional / bool / >max)
  8.  insufficient balance        -> 402, wallet untouched, stock released
  9.  out of stock                -> 422
  10. duplicate request           -> 200 idempotent replay, single debit/stock
  11. concurrent orders           -> same-key race dedupes; stock race never oversells
  12. invalid API key             -> 401
  13. quota exceeded              -> 429 daily_order_quota_exceeded

Also verifies: server-side pricing ignores client price fields (rejected),
quota usage is recorded only for genuinely new orders, and the Phase 11
mapping invariant does not leak product existence.
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase12_orders_", suffix=".db")
os.close(_fd)

import config
config.DB_PATH = _db_path
import database.db as db
db.DB_PATH = _db_path
db._tls = threading.local()
db.init_db()

import database.reseller_db as rdb
from reseller_api import create_app
# Imported explicitly so the patch targets below resolve even when this
# module runs standalone (other suites import them incidentally).
import reseller_api.middleware.request_context  # noqa: F401
import reseller_api.middleware.rate_limit  # noqa: F401
import reseller_api.middleware.security_controls  # noqa: F401
from reseller_api.services.rate_limit import RequestRateDecision

_ctr = 0


def _n():
    global _ctr
    _ctr += 1
    return _ctr


def _make_reseller(name=None, status="active"):
    n = _n()
    name = name or f"P12Res{n}"
    uid = 9_900_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, name, name.lower()),
    )
    db.conn.commit()
    rs = rdb.create_reseller(name, uid, name.lower())
    # Keys can only be minted for an ACTIVE reseller: activate first, mint,
    # then apply the requested lifecycle status.
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    if status != "active":
        rdb.set_reseller_status(rs["id"], status)
    return rs, key_id, secret


def _make_product(name=None, price=10.0, codes=(), max_per_order=1, active=1):
    """Product with uploaded code stock; codes=() makes it manual-stock-free."""
    n = _n()
    name = name or f"P12 Product {n}"
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',?)",
        (name, price, active),
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
    pub = rdb.ensure_product_public_id(pid)
    return pid, pub


def _grant_and_import(rid, pid):
    """Owner access grant + reseller catalog import (both required to order)."""
    rdb.set_product_access(rid, pid, True)
    rdb.import_reseller_product(rid, pid)


def _topup(rid, cents):
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P12-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


def _wallet_balance(rid):
    return rdb.get_wallet_balance_cents(rid)


def _sold_count(pid):
    db.cur.execute(
        "SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=1", (pid,)
    )
    return int(db.cur.fetchone()[0])


def _purchase_debit_count(rid):
    db.cur.execute(
        "SELECT COUNT(*) FROM reseller_wallet_transactions "
        "WHERE reseller_id=? AND tx_type='purchase_debit'",
        (rid,),
    )
    return int(db.cur.fetchone()[0])


def _order_rows(rid, coid):
    db.cur.execute(
        "SELECT id, source, reseller_id, client_order_id, quantity, unit_price_cents "
        "FROM orders WHERE reseller_id=? AND client_order_id=?",
        (rid, coid),
    )
    return db.cur.fetchall()


class Phase12OrderCreationTests(unittest.TestCase):
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

    def _post(self, key, sec, body):
        return self.client.post("/api/v1/orders", headers=_hdr(key, sec), json=body)

    # ---------------------------------------------------------- 1. valid order

    def test_valid_order_debits_wallet_consumes_stock_creates_linked_row(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["C-1", "C-2"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 100000)  # 1000.00

        r = self._post(key, sec, {"product_id": pub, "quantity": 1,
                                  "client_order_id": "p12-valid-1"})
        self.assertEqual(r.status_code, 201, r.get_json())
        data = r.get_json()["data"]
        self.assertRegex(data["order_id"], r"^ORD-\d{8}$")
        self.assertEqual(data["product_id"], pub)
        self.assertEqual(data["quantity"], 1)
        self.assertEqual(data["unit_price"], "10.00")
        self.assertEqual(data["total"], "10.00")
        self.assertEqual(data["currency"], "USDT")
        self.assertEqual(data["client_order_id"], "p12-valid-1")
        self.assertNotIn("idempotent", data)

        # Wallet debited exactly once, ledger linked to the order reference
        self.assertEqual(_wallet_balance(rs["id"]), 100000 - 1000)
        self.assertEqual(_purchase_debit_count(rs["id"]), 1)
        # Stock consumed
        self.assertEqual(_sold_count(pid), 1)
        # Master order row carries the reseller linkage
        rows = _order_rows(rs["id"], "p12-valid-1")
        self.assertEqual(len(rows), 1)
        _oid, source, reseller_id, coid, qty, unit_cents = rows[0]
        self.assertEqual(source, "reseller_api")
        self.assertEqual(reseller_id, rs["id"])
        self.assertEqual(qty, 1)
        self.assertEqual(unit_cents, 1000)

    def test_valid_order_uses_server_side_rule_price(self):
        """A reseller pricing rule — never the client — sets the price."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["C-9"])
        _grant_and_import(rs["id"], pid)
        rdb.set_pricing_rule(rs["id"], pid, "fixed_price", 700)  # 7.00 USDT
        _topup(rs["id"], 100000)

        r = self._post(key, sec, {"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 201, r.get_json())
        data = r.get_json()["data"]
        self.assertEqual(data["unit_price"], "7.00")
        self.assertEqual(data["total"], "7.00")
        self.assertEqual(_wallet_balance(rs["id"]), 100000 - 700)

    def test_client_supplied_price_fields_rejected(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["C-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 100000)
        r = self._post(key, sec, {"product_id": pub, "quantity": 1, "price": "0.01"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"]["code"], "invalid_parameter")

    # ---------------------------------------------------------- 2. invalid product

    def test_malformed_product_id_404(self):
        rs, key, sec = _make_reseller()
        _topup(rs["id"], 10000)
        r = self._post(key, sec, {"product_id": "42", "quantity": 1})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"]["code"], "product_not_found")

    def test_nonexistent_product_404(self):
        rs, key, sec = _make_reseller()
        _topup(rs["id"], 10000)
        r = self._post(key, sec, {"product_id": "PRD-AAAAAAAAAAAA", "quantity": 1})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"]["code"], "product_not_found")

    # ---------------------------------------------------------- 3. not mapped

    def test_access_grant_without_mapping_is_not_orderable(self):
        """Phase 12 core invariant: the orderable catalog is the mapping list."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["C-1"])
        rdb.set_product_access(rs["id"], pid, True)  # grant only, NO import
        _topup(rs["id"], 100000)
        r = self._post(key, sec, {"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"]["code"], "product_not_found")
        # Nothing was debited or consumed
        self.assertEqual(_wallet_balance(rs["id"]), 100000)
        self.assertEqual(_sold_count(pid), 0)

    def test_other_resellers_mapping_does_not_help(self):
        rsA, keyA, secA = _make_reseller()
        rsB, keyB, secB = _make_reseller()
        pid, pub = _make_product(codes=["C-1"])
        _grant_and_import(rsA["id"], pid)          # A imported it
        rdb.set_product_access(rsB["id"], pid, True)  # B has grant only
        _topup(rsB["id"], 100000)
        r = self._post(keyB, secB, {"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"]["code"], "product_not_found")

    # ---------------------------------------------------------- 4/5. disabled states

    def test_disabled_master_product_404(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["C-1"], active=0)
        # Import path rejects inactive products, so seed the mapping directly
        # to simulate "imported while active, disabled later".
        rdb.set_product_access(rs["id"], pid, True)
        db.cur.execute(
            "INSERT INTO reseller_product_mappings(reseller_id, product_id, enabled) "
            "VALUES(?,?,1)", (rs["id"], pid),
        )
        db.conn.commit()
        _topup(rs["id"], 100000)
        r = self._post(key, sec, {"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"]["code"], "product_not_found")

    def test_disabled_mapping_404(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["C-1"])
        _grant_and_import(rs["id"], pid)
        rdb.update_reseller_product_mapping(rs["id"], pid, enabled=False)
        _topup(rs["id"], 100000)
        r = self._post(key, sec, {"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"]["code"], "product_not_found")
        self.assertEqual(_wallet_balance(rs["id"]), 100000)

    # ---------------------------------------------------------- 6. inactive reseller

    def test_suspended_reseller_rejected(self):
        """Suspended resellers fail closed at credential authentication (401).

        The authentication layer deliberately returns a generic 401 for any
        non-active reseller rather than revealing account status (no oracle).
        """
        rs, key, sec = _make_reseller(status="suspended")
        pid, pub = _make_product(codes=["C-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 100000)
        r = self._post(key, sec, {"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()["error"]["code"], "authentication_failed")
        # Nothing was debited or consumed
        self.assertEqual(_wallet_balance(rs["id"]), 100000)
        self.assertEqual(_sold_count(pid), 0)

    # ---------------------------------------------------------- 7. invalid quantity

    def test_invalid_quantities_400(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=[f"C-{i}" for i in range(10)], max_per_order=5)
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 100000)
        for bad in (0, -3, 1.5, "abc", True):
            r = self._post(key, sec, {"product_id": pub, "quantity": bad})
            self.assertEqual(r.status_code, 400, f"quantity={bad!r}")
            self.assertEqual(r.get_json()["error"]["code"], "invalid_quantity",
                             f"quantity={bad!r}")
        # Omitted/null quantity is a missing field, still a 400
        r = self._post(key, sec, {"product_id": pub})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"]["code"], "missing_field")
        # Product max_per_order=5 enforced from the server-side product row
        r = self._post(key, sec, {"product_id": pub, "quantity": 6})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"]["code"], "invalid_quantity")
        # And 5 is accepted
        r = self._post(key, sec, {"product_id": pub, "quantity": 5})
        self.assertEqual(r.status_code, 201, r.get_json())

    # ---------------------------------------------------------- 8. insufficient balance

    def test_insufficient_balance_402_wallet_and_stock_untouched(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["C-1", "C-2"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 500)  # 5.00 < 10.00

        r = self._post(key, sec, {"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.get_json()["error"]["code"], "insufficient_balance")
        # Wallet not debited, no order row, reserved stock was released
        self.assertEqual(_wallet_balance(rs["id"]), 500)
        self.assertEqual(_purchase_debit_count(rs["id"]), 0)
        self.assertEqual(_sold_count(pid), 0)
        db.cur.execute(
            "SELECT COUNT(*) FROM orders WHERE reseller_id=?", (rs["id"],)
        )
        self.assertEqual(int(db.cur.fetchone()[0]), 0)

    # ---------------------------------------------------------- 9. out of stock

    def test_out_of_stock_422(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=())  # no codes, no manual stock
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 100000)
        r = self._post(key, sec, {"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.get_json()["error"]["code"], "out_of_stock")

    def test_insufficient_stock_422(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["C-1"], max_per_order=10)
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 100000)
        r = self._post(key, sec, {"product_id": pub, "quantity": 2})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.get_json()["error"]["code"], "insufficient_stock")
        self.assertEqual(_sold_count(pid), 0)

    # ---------------------------------------------------------- 10. duplicate request

    def test_duplicate_client_order_id_replays_without_double_debit(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["C-1", "C-2"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 100000)

        r1 = self._post(key, sec, {"product_id": pub, "quantity": 1,
                                   "client_order_id": "p12-dup-1"})
        self.assertEqual(r1.status_code, 201, r1.get_json())
        r2 = self._post(key, sec, {"product_id": pub, "quantity": 1,
                                   "client_order_id": "p12-dup-1"})
        self.assertEqual(r2.status_code, 200, r2.get_json())
        d2 = r2.get_json()["data"]
        self.assertTrue(d2["idempotent"])
        self.assertEqual(d2["order_id"], r1.get_json()["data"]["order_id"])
        # Exactly one order row, one debit, one stock unit
        self.assertEqual(len(_order_rows(rs["id"], "p12-dup-1")), 1)
        self.assertEqual(_purchase_debit_count(rs["id"]), 1)
        self.assertEqual(_wallet_balance(rs["id"]), 100000 - 1000)
        self.assertEqual(_sold_count(pid), 1)

    # ---------------------------------------------------------- 11. concurrency

    def test_concurrent_same_client_order_id_single_order(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["C-1", "C-2"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 100000)

        results = []

        def worker():
            client = self.app.test_client()
            r = client.post(
                "/api/v1/orders", headers=_hdr(key, sec),
                json={"product_id": pub, "quantity": 1,
                      "client_order_id": "p12-race-1"},
            )
            results.append((r.status_code, r.get_json()))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        codes = sorted(c for c, _ in results)
        self.assertEqual(codes, [200, 201], results)
        order_ids = {d["data"]["order_id"] for _, d in results}
        self.assertEqual(len(order_ids), 1)
        self.assertEqual(len(_order_rows(rs["id"], "p12-race-1")), 1)
        self.assertEqual(_purchase_debit_count(rs["id"]), 1)
        self.assertEqual(_wallet_balance(rs["id"]), 100000 - 1000)
        self.assertEqual(_sold_count(pid), 1)

    def test_concurrent_distinct_orders_never_oversell(self):
        """6 racing buyers, 4 units: exactly 4 win, stock ends at 0, never negative."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=[f"C-{i}" for i in range(4)])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 100000)

        results = []
        with patch(
            "reseller_api.services.financial_abuse_guard.PRODUCT_BURST_MAX", 100
        ):
            def worker(i):
                client = self.app.test_client()
                r = client.post(
                    "/api/v1/orders", headers=_hdr(key, sec),
                    json={"product_id": pub, "quantity": 1,
                          "client_order_id": f"p12-race-x{i}"},
                )
                results.append(r.status_code)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(sorted(results).count(201), 4, results)
        self.assertEqual(len([c for c in results if c == 422]), 2, results)
        self.assertEqual(_sold_count(pid), 4)
        self.assertEqual(_wallet_balance(rs["id"]), 100000 - 4 * 1000)
        db.cur.execute(
            "SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=0", (pid,)
        )
        self.assertEqual(int(db.cur.fetchone()[0]), 0)

    # ---------------------------------------------------------- 12. invalid API key

    def test_invalid_api_key_401(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["C-1"])
        _grant_and_import(rs["id"], pid)
        r = self._post(key, "definitely-wrong-secret",
                       {"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()["error"]["code"], "authentication_failed")

    def test_missing_api_key_401(self):
        r = self.client.post("/api/v1/orders",
                             json={"product_id": "PRD-AAAAAAAAAAAA", "quantity": 1})
        self.assertEqual(r.status_code, 401)

    # ---------------------------------------------------------- 13. quota

    def test_daily_quota_exceeded_429(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["C-1", "C-2"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 100000)
        rdb.set_quota_settings(rs["id"], daily_order_quota=1)

        r1 = self._post(key, sec, {"product_id": pub, "quantity": 1,
                                   "client_order_id": "p12-quota-1"})
        self.assertEqual(r1.status_code, 201, r1.get_json())

        r2 = self._post(key, sec, {"product_id": pub, "quantity": 1,
                                   "client_order_id": "p12-quota-2"})
        self.assertEqual(r2.status_code, 429, r2.get_json())
        self.assertEqual(r2.get_json()["error"]["code"], "daily_order_quota_exceeded")
        # The rejected order consumed nothing
        self.assertEqual(_wallet_balance(rs["id"]), 100000 - 1000)
        self.assertEqual(_sold_count(pid), 1)

    def test_idempotent_replay_does_not_consume_quota(self):
        """A duplicate replay must not eat the daily quota a second time."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["C-1", "C-2"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 100000)
        rdb.set_quota_settings(rs["id"], daily_order_quota=2)

        body = {"product_id": pub, "quantity": 1, "client_order_id": "p12-quota-dup"}
        r1 = self._post(key, sec, body)
        self.assertEqual(r1.status_code, 201, r1.get_json())
        r2 = self._post(key, sec, body)  # replay — no new usage
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.get_json()["data"]["idempotent"])
        # A genuinely new order still fits inside the quota of 2
        r3 = self._post(key, sec, {"product_id": pub, "quantity": 1,
                                   "client_order_id": "p12-quota-new"})
        self.assertEqual(r3.status_code, 201, r3.get_json())


if __name__ == "__main__":
    unittest.main()
