"""Phase 12 Prompt 6 — reseller order status & lifecycle API.

Verifies the reseller-safe status surface on top of the EXISTING master
order lifecycle (no invented states):

  - order detail shape + internal-field hygiene + truthful updated_at
  - order list ordering and cursor pagination (has_more / next_after_id)
  - filtering: client_order_id, product_id, date range, status
  - status values: completed (default), failed, cancelled
  - rejected pseudo-states (refunded / paid) -> 400
  - refund visibility from the reseller's own ledger (full / cancellation /
    partial), introduced by the P4-repaired failure handlers
  - invalid order IDs, cross-reseller isolation (404, no oracle)
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase12_lifecycle_", suffix=".db")
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
    name = name or f"P12LRes{n}"
    uid = 9_995_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, name, name.lower()),
    )
    db.conn.commit()
    rs = rdb.create_reseller(name, uid, name.lower())
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=("L-1",), max_per_order=1):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P12L Product {n}", price),
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P12L-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


def _internal(public_oid):
    return int(public_oid.replace("ORD-", ""))


class Phase12OrderLifecycleTests(unittest.TestCase):
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
        r = self.client.post("/api/v1/orders", headers=_hdr(key, sec), json=body)
        assert r.status_code == 201, r.get_json()
        return r.get_json()["data"]

    def _detail(self, key, sec, oid):
        return self.client.get(f"/api/v1/orders/{oid}", headers=_hdr(key, sec))

    def _list(self, key, sec, qs=""):
        return self.client.get(f"/api/v1/orders{qs}", headers=_hdr(key, sec))

    # ------------------------------------------------------------ detail

    def test_order_detail_shape_and_internal_hygiene(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        data = self._order(key, sec, pub, 1, "life-detail")

        r = self._detail(key, sec, data["order_id"])
        self.assertEqual(r.status_code, 200, r.get_json())
        o = r.get_json()["data"]["order"]
        # reseller-safe fields
        for field in ("order_id", "product_id", "quantity", "unit_price",
                      "total", "currency", "status", "created_at",
                      "updated_at", "client_order_id", "delivery_status",
                      "refunded"):
            self.assertIn(field, o, field)
        self.assertEqual(o["status"], "completed")
        self.assertEqual(o["delivery_status"], "delivered")
        self.assertFalse(o["refunded"])
        self.assertNotIn("refund", o)
        # untouched order: updated_at falls back to created_at
        self.assertEqual(o["updated_at"], o["created_at"])
        # nothing internal leaks
        for leaked in ("id", "reseller_id", "reseller_key_id", "request_id",
                       "user_id", "failure_reason", "master_status",
                       "stock_restored", "source"):
            self.assertNotIn(leaked, o)

    # ------------------------------------------------------------ list + pagination

    def test_order_list_desc_and_pagination(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=("L-1", "L-2", "L-3"))
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        ids = [self._order(key, sec, pub, 1, f"life-page-{i}")["order_id"]
               for i in range(3)]

        r1 = self._list(key, sec, "?limit=2")
        self.assertEqual(r1.status_code, 200, r1.get_json())
        page1 = r1.get_json()["data"]["orders"]
        meta1 = r1.get_json()["meta"]["pagination"]
        self.assertEqual(len(page1), 2)
        self.assertTrue(meta1["has_more"])
        self.assertEqual(meta1["next_after_id"], page1[-1]["order_id"])
        # DESC by id: newest first
        self.assertEqual([o["order_id"] for o in page1], [ids[2], ids[1]])

        r2 = self._list(key, sec, f"?limit=2&after_id={meta1['next_after_id']}")
        page2 = r2.get_json()["data"]["orders"]
        meta2 = r2.get_json()["meta"]["pagination"]
        self.assertEqual([o["order_id"] for o in page2], [ids[0]])
        self.assertFalse(meta2["has_more"])

        # limit ceiling enforced
        r3 = self._list(key, sec, "?limit=101")
        self.assertEqual(r3.status_code, 400)
        self.assertEqual(r3.get_json()["error"]["code"], "invalid_pagination")

    # ------------------------------------------------------------ filtering

    def test_filters_client_product_date(self):
        rs, key, sec = _make_reseller()
        pidA, pubA = _make_product(codes=("L-A1",))
        pidB, pubB = _make_product(codes=("L-B1",))
        _grant_and_import(rs["id"], pidA)
        _grant_and_import(rs["id"], pidB)
        _topup(rs["id"], 50000)
        oA = self._order(key, sec, pubA, 1, "life-fA")
        oB = self._order(key, sec, pubB, 1, "life-fB")

        r = self._list(key, sec, "?client_order_id=life-fA")
        got = r.get_json()["data"]["orders"]
        self.assertEqual([o["order_id"] for o in got], [oA["order_id"]])

        r = self._list(key, sec, f"?product_id={pubB}")
        got = r.get_json()["data"]["orders"]
        self.assertEqual([o["order_id"] for o in got], [oB["order_id"]])

        # created is stored in DB time (UTC); derive the filter day from the
        # stored row itself so the assertion is timezone-proof.
        db.cur.execute("SELECT substr(created,1,10) FROM orders WHERE id=?",
                       (_internal(oA["order_id"]),))
        day = db.cur.fetchone()[0]
        r = self._list(key, sec, f"?date_from={day}&date_to={day}")
        self.assertEqual(len(r.get_json()["data"]["orders"]), 2)
        r = self._list(key, sec, "?date_from=2999-01-01")
        self.assertEqual(r.get_json()["data"]["orders"], [])

    def test_status_filter(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=("L-1", "L-2", "L-3"))
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        ok = self._order(key, sec, pub, 1, "life-s-ok")
        bad = self._order(key, sec, pub, 1, "life-s-bad")
        can = self._order(key, sec, pub, 1, "life-s-can")
        handle_post_debit_failure(_internal(bad["order_id"]), reason="supplier_error")
        handle_cancellation(_internal(can["order_id"]), post_debit=True)

        # Prompt 16.5 explicitly requires presence-filter readiness.
        from reseller_api.services.delivery_normalization import normalize_empty_deliveries
        self.assertTrue(normalize_empty_deliveries(apply=True)['ready'])
        r = self._list(key, sec, "?status=processing")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['data']['orders'], [])
        r = self._list(key, sec, "?status=completed")
        self.assertEqual([o["order_id"] for o in r.get_json()["data"]["orders"]],
                         [ok["order_id"]])
        r = self._list(key, sec, "?status=failed")
        self.assertEqual([o["order_id"] for o in r.get_json()["data"]["orders"]],
                         [bad["order_id"]])
        r = self._list(key, sec, "?status=cancelled")
        self.assertEqual([o["order_id"] for o in r.get_json()["data"]["orders"]],
                         [can["order_id"]])
        # pseudo-states that the master lifecycle does not have are rejected
        for bogus in ("refunded", "paid", "fulfilling", "pending"):
            r = self._list(key, sec, f"?status={bogus}")
            self.assertEqual(r.status_code, 400, bogus)
            self.assertEqual(r.get_json()["error"]["code"], "invalid_filter")

    # ------------------------------------------------------------ states

    def test_completed_order(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        data = self._order(key, sec, pub, 1, "life-ok")
        o = self._detail(key, sec, data["order_id"]).get_json()["data"]["order"]
        self.assertEqual(o["status"], "completed")
        self.assertEqual(o["delivery_status"], "delivered")
        self.assertFalse(o["refunded"])

    def test_failed_order_with_full_refund(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        data = self._order(key, sec, pub, 1, "life-fail")
        oid = _internal(data["order_id"])
        handle_post_debit_failure(oid, reason="supplier_error")

        o = self._detail(key, sec, data["order_id"]).get_json()["data"]["order"]
        self.assertEqual(o["status"], "failed")
        self.assertEqual(o["delivery_status"], "delivered")  # blob exists; access blocked by delivery endpoint
        self.assertTrue(o["refunded"])
        self.assertEqual(o["refund"]["total"], "10.00")
        self.assertEqual(o["refund"]["currency"], "USDT")
        self.assertEqual(o["refund"]["type"], "full")
        self.assertTrue(o["refund"]["refunded_at"])
        # terminal transition wrote the updated timestamp
        db.cur.execute("SELECT updated FROM orders WHERE id=?", (oid,))
        self.assertTrue(db.cur.fetchone()[0])
        # delivery endpoint blocks content on failed orders
        dv = self.client.get(f"/api/v1/orders/{data['order_id']}/delivery",
                             headers=_hdr(key, sec))
        self.assertEqual(dv.get_json()["data"]["delivery"]["delivery_status"], "failed")
        self.assertEqual(dv.get_json()["data"]["delivery"]["items"], [])

    def test_cancelled_order_with_cancellation_refund(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        data = self._order(key, sec, pub, 1, "life-cancel")
        handle_cancellation(_internal(data["order_id"]), post_debit=True)

        o = self._detail(key, sec, data["order_id"]).get_json()["data"]["order"]
        self.assertEqual(o["status"], "cancelled")
        self.assertTrue(o["refunded"])
        self.assertEqual(o["refund"]["type"], "cancellation")
        self.assertEqual(o["refund"]["total"], "10.00")

    def test_partial_refund_order_stays_completed(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=("L-1", "L-2"), max_per_order=5)
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        data = self._order(key, sec, pub, 2, "life-partial")
        handle_partial_failure(_internal(data["order_id"]), 1, 1)

        o = self._detail(key, sec, data["order_id"]).get_json()["data"]["order"]
        self.assertEqual(o["status"], "completed")   # partially fulfilled, not failed
        self.assertTrue(o["refunded"])
        self.assertEqual(o["refund"]["total"], "10.00")  # one of two units
        self.assertEqual(o["refund"]["type"], "partial")

    # ------------------------------------------------------------ safety

    def test_invalid_and_foreign_order_ids(self):
        rsA, keyA, secA = _make_reseller()
        rsB, keyB, secB = _make_reseller()
        pid, pub = _make_product()
        _grant_and_import(rsA["id"], pid)
        _topup(rsA["id"], 50000)
        data = self._order(keyA, secA, pub, 1, "life-iso")

        # malformed -> 400, well-formed but unknown -> 404
        self.assertEqual(self._detail(keyA, secA, "ORD-1").status_code, 400)
        self.assertEqual(self._detail(keyA, secA, "ORD-99999999").status_code, 404)
        # another reseller's order -> 404 (no existence oracle)
        r = self._detail(keyB, secB, data["order_id"])
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"]["code"], "order_not_found")


if __name__ == "__main__":
    unittest.main()
