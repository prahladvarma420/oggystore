"""Phase 12 Prompt 3 — HTTP tests for POST /api/v1/orders/quote.

Verifies the quote layer is server-authoritative and side-effect free:
  1.  normal quote              -> 200 with product/qty/unit/total/currency
  2.  multiple quantity         -> total = unit * qty (integer cents math)
  3.  invalid quantity          -> 400
  4.  inactive product          -> 404 product_not_found
  5.  price change after quote  -> order recalculates (old quote never trusted)
  6.  reseller markup           -> rule:markup_percent reflected in quote
  7.  insufficient balance      -> quote 200 + sufficient_funds=false; order -> 402
  8.  currency                  -> always USDT, display strings, no floats
  9.  manipulated quote         -> client price/cost/markup/discount rejected
  10. stale quote               -> quote is not a reservation; order re-checks
  +   quotes never reserve stock, debit the wallet, create orders, or eat quota
  +   mapping invariant and auth apply to quotes exactly as to orders
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase12_quotes_", suffix=".db")
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
    name = name or f"P12QRes{n}"
    uid = 9_950_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, name, name.lower()),
    )
    db.conn.commit()
    rs = rdb.create_reseller(name, uid, name.lower())
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(name=None, price=10.0, codes=(), max_per_order=1, active=1):
    n = _n()
    name = name or f"P12Q Product {n}"
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
    rdb.set_product_access(rid, pid, True)
    rdb.import_reseller_product(rid, pid)


def _topup(rid, cents):
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P12Q-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


class Phase12QuoteTests(unittest.TestCase):
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

    def _quote(self, key, sec, body):
        return self.client.post("/api/v1/orders/quote", headers=_hdr(key, sec), json=body)

    def _order(self, key, sec, body):
        return self.client.post("/api/v1/orders", headers=_hdr(key, sec), json=body)

    # ------------------------------------------------------------- normal quote

    def test_normal_quote_is_authoritative_and_side_effect_free(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["Q-1", "Q-2"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)  # 500.00

        r = self._quote(key, sec, {"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()["data"]
        self.assertEqual(d["product_id"], pub)
        self.assertEqual(d["quantity"], 1)
        self.assertEqual(d["unit_price"], "10.00")
        self.assertEqual(d["total"], "10.00")
        self.assertEqual(d["currency"], "USDT")
        self.assertEqual(d["price_source"], "master")
        self.assertEqual(d["available_quantity"], 2)
        self.assertTrue(d["in_stock"])
        self.assertEqual(d["wallet_balance"], "500.00")
        self.assertTrue(d["sufficient_funds"])
        self.assertTrue(d["quoted_at"])

        # Zero side effects: no stock claim, no debit, no order, no quota event
        db.cur.execute("SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=1", (pid,))
        self.assertEqual(int(db.cur.fetchone()[0]), 0)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)
        db.cur.execute("SELECT COUNT(*) FROM orders WHERE reseller_id=?", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 0)
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_financial_events WHERE reseller_id=? AND event_type='order_placed'",
            (rs["id"],),
        )
        self.assertEqual(int(db.cur.fetchone()[0]), 0)

    def test_multiple_quantity_quote(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=7.5, codes=["Q-1", "Q-2", "Q-3", "Q-4"], max_per_order=5)
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._quote(key, sec, {"product_id": pub, "quantity": 3})
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()["data"]
        self.assertEqual(d["unit_price"], "7.50")
        self.assertEqual(d["total"], "22.50")  # 750 * 3 integer cents
        self.assertEqual(d["quantity"], 3)
        self.assertEqual(d["max_per_order"], 5)

    # ------------------------------------------------------------- invalid quantity

    def test_invalid_quantity_400(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["Q-1", "Q-2"], max_per_order=2)
        _grant_and_import(rs["id"], pid)
        for bad in (0, -1, 1.5, "two", True):
            r = self._quote(key, sec, {"product_id": pub, "quantity": bad})
            self.assertEqual(r.status_code, 400, f"quantity={bad!r}")
            self.assertEqual(r.get_json()["error"]["code"], "invalid_quantity")
        r = self._quote(key, sec, {"product_id": pub, "quantity": 3})  # > max_per_order
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"]["code"], "invalid_quantity")
        r = self._quote(key, sec, {"product_id": pub})  # omitted
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"]["code"], "missing_field")

    # ------------------------------------------------------------- inactive / unmapped

    def test_inactive_product_404(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["Q-1"], active=0)
        rdb.set_product_access(rs["id"], pid, True)
        db.cur.execute(
            "INSERT INTO reseller_product_mappings(reseller_id, product_id, enabled) VALUES(?,?,1)",
            (rs["id"], pid),
        )
        db.conn.commit()
        r = self._quote(key, sec, {"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"]["code"], "product_not_found")

    def test_unmapped_product_404(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["Q-1"])
        rdb.set_product_access(rs["id"], pid, True)  # grant only, no mapping
        r = self._quote(key, sec, {"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"]["code"], "product_not_found")

    # ------------------------------------------------------------- price change

    def test_price_change_after_quote_order_charges_new_price(self):
        """The order endpoint must NEVER trust an old quote: owner repricing
        between quote and order must be reflected in the actual charge."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["Q-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)

        q1 = self._quote(key, sec, {"product_id": pub, "quantity": 1})
        self.assertEqual(q1.get_json()["data"]["total"], "10.00")

        # Owner reprices the master product
        db.cur.execute("UPDATE products SET price=? WHERE id=?", (12.5, pid))
        db.conn.commit()

        q2 = self._quote(key, sec, {"product_id": pub, "quantity": 1})
        self.assertEqual(q2.get_json()["data"]["total"], "12.50")

        r = self._order(key, sec, {"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(r.get_json()["data"]["total"], "12.50")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000 - 1250)

    # ------------------------------------------------------------- reseller markup

    def test_reseller_markup_rule_reflected(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["Q-1"])
        _grant_and_import(rs["id"], pid)
        rdb.set_pricing_rule(rs["id"], pid, "markup_percent", 1000)  # +10%
        _topup(rs["id"], 50000)
        r = self._quote(key, sec, {"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()["data"]
        self.assertEqual(d["unit_price"], "11.00")
        self.assertEqual(d["total"], "11.00")
        self.assertEqual(d["price_source"], "rule:markup_percent")

    # ------------------------------------------------------------- insufficient balance

    def test_quote_surfaces_insufficient_funds_then_order_402(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["Q-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 500)  # 5.00 < 10.00

        q = self._quote(key, sec, {"product_id": pub, "quantity": 1})
        self.assertEqual(q.status_code, 200, q.get_json())
        d = q.get_json()["data"]
        self.assertEqual(d["total"], "10.00")
        self.assertEqual(d["wallet_balance"], "5.00")
        self.assertFalse(d["sufficient_funds"])

        # The quote is not a loophole: the real order still fails closed
        r = self._order(key, sec, {"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.get_json()["error"]["code"], "insufficient_balance")
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 500)

    # ------------------------------------------------------------- currency

    def test_currency_model(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=19.99, codes=["Q-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._quote(key, sec, {"product_id": pub, "quantity": 1})
        d = r.get_json()["data"]
        self.assertEqual(d["currency"], "USDT")
        # Money is rendered as decimal strings (no floats on the money path)
        self.assertIsInstance(d["unit_price"], str)
        self.assertIsInstance(d["total"], str)
        self.assertIsInstance(d["wallet_balance"], str)
        self.assertEqual(d["unit_price"], "19.99")

    # ------------------------------------------------------------- manipulated quote

    def test_client_pricing_fields_rejected(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["Q-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        for field in ("price", "unit_price", "total", "total_cents", "cost",
                      "markup", "discount"):
            r = self._quote(key, sec, {"product_id": pub, "quantity": 1, field: "0.01"})
            self.assertEqual(r.status_code, 400, f"field={field}")
            self.assertEqual(r.get_json()["error"]["code"], "invalid_parameter",
                             f"field={field}")

    # ------------------------------------------------------------- stale quote

    def test_stale_quote_stock_gone_order_422(self):
        """A quote is not a reservation: stock can vanish between quote and
        order, and the order must then fail closed with 422."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["Q-ONLY"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)

        q = self._quote(key, sec, {"product_id": pub, "quantity": 1})
        self.assertEqual(q.status_code, 200)
        self.assertTrue(q.get_json()["data"]["in_stock"])

        # Someone else buys the last unit after the quote
        db.cur.execute("UPDATE product_stock SET sold=1 WHERE product_id=?", (pid,))
        db.conn.commit()

        # A fresh quote now fails closed...
        q2 = self._quote(key, sec, {"product_id": pub, "quantity": 1})
        self.assertEqual(q2.status_code, 422)
        self.assertEqual(q2.get_json()["error"]["code"], "out_of_stock")
        # ...and the stale quote cannot force the order through
        r = self._order(key, sec, {"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 50000)

    def test_quote_does_not_consume_quota(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["Q-1"])
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        rdb.set_quota_settings(rs["id"], daily_order_quota=1)

        for _ in range(3):
            q = self._quote(key, sec, {"product_id": pub, "quantity": 1})
            self.assertEqual(q.status_code, 200)
        # Quota is still intact for the real order
        r = self._order(key, sec, {"product_id": pub, "quantity": 1})
        self.assertEqual(r.status_code, 201, r.get_json())

    # ------------------------------------------------------------- auth

    def test_quote_requires_valid_key(self):
        r = self._quote("rk_test_NOPE", "bad-secret",
                        {"product_id": "PRD-AAAAAAAAAAAA", "quantity": 1})
        self.assertEqual(r.status_code, 401)
        r2 = self.client.post("/api/v1/orders/quote",
                              json={"product_id": "PRD-AAAAAAAAAAAA", "quantity": 1})
        self.assertEqual(r2.status_code, 401)


if __name__ == "__main__":
    unittest.main()
