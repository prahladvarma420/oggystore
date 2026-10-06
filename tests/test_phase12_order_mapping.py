"""Phase 12 Prompt 5 — reseller order <-> master order mapping.

The architecture is a SINGLE orders table: the reseller order IS the master
order row (source='reseller_api', reseller_id, reseller_key_id,
client_order_id, request_id, quantity, unit_price_cents), publicly identified
as ORD-{id:08d}. These tests verify the mapping is traceable, unique,
indexed, secure, auditable — and that the owner can identify both sides of
the link from WebAdmin (without any reseller gaining WebAdmin access).

Covers: normal mapping, duplicate mapping, order lookup, reseller isolation,
invalid master order, failed master order, retry, concurrent order,
API restart, and the WebAdmin owner views.
"""

import os
import re
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase12_mapping_", suffix=".db")
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

# WebAdmin shares the same database file (import-time init is idempotent).
import web_admin.app as wa

_ctr = 0


def _n():
    global _ctr
    _ctr += 1
    return _ctr


def _make_reseller(name=None):
    n = _n()
    name = name or f"P12MRes{n}"
    uid = 9_990_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, name, name.lower()),
    )
    db.conn.commit()
    rs = rdb.create_reseller(name, uid, name.lower())
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=("M-1",)):
    n = _n()
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,0,'code_stock',1)",
        (f"P12M Product {n}", price),
    )
    db.conn.commit()
    pid = db.cur.lastrowid
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P12M-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


class Phase12OrderMappingTests(unittest.TestCase):
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
            pass  # DB file removed after the WebAdmin class finishes too

    def _order(self, key, sec, pub, qty=1, coid=None, client=None):
        body = {"product_id": pub, "quantity": qty}
        if coid:
            body["client_order_id"] = coid
        return (client or self.client).post(
            "/api/v1/orders", headers=_hdr(key, sec), json=body)

    # ---------------------------------------------------------- normal mapping

    def test_normal_mapping_traceability(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)

        r = self._order(key, sec, pub, 1, "map-1")
        self.assertEqual(r.status_code, 201, r.get_json())
        data = r.get_json()["data"]
        public_oid = data["order_id"]
        self.assertRegex(public_oid, r"^ORD-\d{8}$")
        internal_id = int(public_oid.replace("ORD-", ""))

        # The master order row carries the full reseller linkage
        db.cur.execute(
            "SELECT source, reseller_id, reseller_key_id, client_order_id,"
            " request_id, quantity, unit_price_cents, user_id, product_id"
            " FROM orders WHERE id=?", (internal_id,),
        )
        (source, rid, key_id, coid, req_id, qty, unit_cents,
         user_id, product_id) = db.cur.fetchone()
        self.assertEqual(source, "reseller_api")
        self.assertEqual(rid, rs["id"])
        self.assertEqual(key_id, key)          # public key id, never the secret
        self.assertNotEqual(key_id, sec)
        self.assertEqual(coid, "map-1")
        self.assertTrue(req_id)
        self.assertEqual((qty, unit_cents), (1, 1000))
        self.assertEqual(user_id, 0)           # reseller orders never impersonate customers
        self.assertEqual(product_id, pid)

        # The public API never exposes internal identifiers
        for leaked in ("id", "reseller_id", "reseller_key_id", "request_id",
                       "user_id"):
            self.assertNotIn(leaked, data)

        # GET detail round-trips the same public id
        g = self.client.get(f"/api/v1/orders/{public_oid}", headers=_hdr(key, sec))
        self.assertEqual(g.status_code, 200, g.get_json())
        self.assertEqual(g.get_json()["data"]["order"]["order_id"], public_oid)

    # ---------------------------------------------------------- duplicate mapping

    def test_duplicate_mapping_single_row(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=("M-1", "M-2"))
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r1 = self._order(key, sec, pub, 1, "map-dup")
        r2 = self._order(key, sec, pub, 1, "map-dup")
        self.assertEqual((r1.status_code, r2.status_code), (201, 200))
        self.assertEqual(r1.get_json()["data"]["order_id"],
                         r2.get_json()["data"]["order_id"])
        db.cur.execute(
            "SELECT COUNT(*) FROM orders WHERE reseller_id=? AND client_order_id=?",
            (rs["id"], "map-dup"),
        )
        self.assertEqual(int(db.cur.fetchone()[0]), 1)

    # ---------------------------------------------------------- lookup + isolation

    def test_order_lookup_fields(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._order(key, sec, pub, 1, "map-lookup")
        oid = r.get_json()["data"]["order_id"]
        g = self.client.get(f"/api/v1/orders/{oid}", headers=_hdr(key, sec))
        d = g.get_json()["data"]["order"]
        self.assertEqual(d["order_id"], oid)
        self.assertEqual(d["product_id"], pub)
        self.assertEqual(d["quantity"], 1)
        self.assertEqual(d["unit_price"], "10.00")
        self.assertEqual(d["total"], "10.00")
        self.assertEqual(d["currency"], "USDT")
        self.assertEqual(d["status"], "completed")
        self.assertEqual(d["client_order_id"], "map-lookup")
        self.assertEqual(d["delivery_status"], "delivered")
        self.assertNotIn("id", d)
        self.assertNotIn("reseller_id", d)

    def test_reseller_isolation_detail_list_delivery(self):
        rsA, keyA, secA = _make_reseller()
        rsB, keyB, secB = _make_reseller()
        pid, pub = _make_product(codes=("M-1", "M-2"))
        _grant_and_import(rsA["id"], pid)
        _grant_and_import(rsB["id"], pid)
        _topup(rsA["id"], 50000)
        _topup(rsB["id"], 50000)
        rA = self._order(keyA, secA, pub, 1, "map-iso-A")
        oidA = rA.get_json()["data"]["order_id"]

        # B cannot read A's order (404, indistinguishable from nonexistent)
        g = self.client.get(f"/api/v1/orders/{oidA}", headers=_hdr(keyB, secB))
        self.assertEqual(g.status_code, 404)
        self.assertEqual(g.get_json()["error"]["code"], "order_not_found")
        # B cannot read A's delivery
        dv = self.client.get(f"/api/v1/orders/{oidA}/delivery", headers=_hdr(keyB, secB))
        self.assertEqual(dv.status_code, 404)
        # B's list does not contain A's order (even filtered by its client id)
        lst = self.client.get("/api/v1/orders?client_order_id=map-iso-A",
                              headers=_hdr(keyB, secB))
        self.assertEqual(lst.status_code, 200)
        self.assertEqual(lst.get_json()["data"]["orders"], [])
        # A's list contains exactly that order
        lstA = self.client.get("/api/v1/orders?client_order_id=map-iso-A",
                               headers=_hdr(keyA, secA))
        self.assertEqual(len(lstA.get_json()["data"]["orders"]), 1)

    # ---------------------------------------------------------- invalid / failed

    def test_invalid_master_order_ids(self):
        rs, key, sec = _make_reseller()
        for bad, want in (("ORD-99999999", 404), ("ORD-123", 400),
                          ("garbage", 400)):
            r = self.client.get(f"/api/v1/orders/{bad}", headers=_hdr(key, sec))
            self.assertEqual(r.status_code, want, f"id={bad!r} -> {r.status_code}")

    def test_failed_master_order_status_is_truthful(self):
        """A failed reseller order must READ as failed — not 'completed'."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product()
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._order(key, sec, pub, 1, "map-fail")
        oid = r.get_json()["data"]["order_id"]
        internal = int(oid.replace("ORD-", ""))

        from reseller_api.services.order_failure import handle_post_debit_failure
        handle_post_debit_failure(internal, reason="supplier_error")

        g = self.client.get(f"/api/v1/orders/{oid}", headers=_hdr(key, sec))
        self.assertEqual(g.status_code, 200)
        self.assertEqual(g.get_json()["data"]["order"]["status"], "failed")
        # Delivery endpoint agrees
        dv = self.client.get(f"/api/v1/orders/{oid}/delivery", headers=_hdr(key, sec))
        dd = dv.get_json()["data"]["delivery"]
        self.assertEqual(dd["delivery_status"], "failed")
        self.assertEqual(dd["items"], [])
        # List view agrees too
        lst = self.client.get("/api/v1/orders?client_order_id=map-fail",
                              headers=_hdr(key, sec))
        self.assertEqual(lst.get_json()["data"]["orders"][0]["status"], "failed")

    # ---------------------------------------------------------- retry / race / restart

    def test_retry_returns_same_mapping(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=("M-1", "M-2"))
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r1 = self._order(key, sec, pub, 1, "map-retry")
        r2 = self._order(key, sec, pub, 1, "map-retry")
        self.assertEqual(r1.get_json()["data"]["order_id"],
                         r2.get_json()["data"]["order_id"])

    def test_concurrent_order_single_mapping(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=("M-1", "M-2"))
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        results = []

        def worker():
            c = self.app.test_client()
            results.append(self._order(key, sec, pub, 1, "map-race", client=c).status_code)

        ts = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(results), [200, 201], results)
        db.cur.execute(
            "SELECT COUNT(*) FROM orders WHERE reseller_id=? AND client_order_id=?",
            (rs["id"], "map-race"),
        )
        self.assertEqual(int(db.cur.fetchone()[0]), 1)

    def test_api_restart_persistence(self):
        """A fresh app instance (new worker after restart) sees the same
        mapping: order retrievable, idempotency still enforced."""
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=("M-1", "M-2"))
        _grant_and_import(rs["id"], pid)
        _topup(rs["id"], 50000)
        r1 = self._order(key, sec, pub, 1, "map-restart")
        self.assertEqual(r1.status_code, 201)
        oid = r1.get_json()["data"]["order_id"]

        # Simulate a worker restart: brand-new Flask app + client, same DB.
        app2 = create_app({
            "TESTING": True, "API_ENVIRONMENT": "test",
            "INIT_DATABASE": False, "RESELLER_API_HSTS": False,
        })
        client2 = app2.test_client()
        g = client2.get(f"/api/v1/orders/{oid}", headers=_hdr(key, sec))
        self.assertEqual(g.status_code, 200)
        self.assertEqual(g.get_json()["data"]["order"]["order_id"], oid)
        # Idempotent replay still resolves to the same order after restart
        r2 = self._order(key, sec, pub, 1, "map-restart", client=client2)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.get_json()["data"]["order_id"], oid)
        self.assertTrue(r2.get_json()["data"]["idempotent"])


class Phase12WebAdminMappingTests(unittest.TestCase):
    """Owner-side identification of master <-> reseller orders."""

    @classmethod
    def setUpClass(cls):
        cls.admin = wa.app.test_client()
        with cls.admin.session_transaction() as s:
            s["logged_in"] = True
            s["web_role"] = "owner"
            s["last_active"] = time.time()
        # Seed one API order through the REAL service layer
        rs, key, sec = _make_reseller("P12M AdminView")
        cls.reseller = rs
        pid, pub = _make_product()
        rdb.set_product_access(rs["id"], pid, True)
        rdb.import_reseller_product(rs["id"], pid)
        _topup(rs["id"], 50000)
        cls.pid, cls.pub = pid, pub
        cls.key, cls.sec = key, sec

        # Place the order via the reseller API app (full stack)
        with patch("reseller_api.middleware.request_context.persist_audit_record", return_value=True), \
             patch("reseller_api.middleware.rate_limit.evaluate_request_rate",
                   return_value=RequestRateDecision(True, "allowed", 60, 59, 45)), \
             patch("reseller_api.middleware.security_controls.is_global_api_enabled", return_value=True):
            app = create_app({"TESTING": True, "API_ENVIRONMENT": "test",
                              "INIT_DATABASE": False, "RESELLER_API_HSTS": False})
            rc = app.test_client()
            r = rc.post("/api/v1/orders", headers=_hdr(key, sec),
                        json={"product_id": pub, "quantity": 1,
                              "client_order_id": "admin-view-1"})
            assert r.status_code == 201, r.get_json()
            cls.public_oid = r.get_json()["data"]["order_id"]
            cls.internal_id = int(cls.public_oid.replace("ORD-", ""))

    def test_admin_orders_show_reseller_attribution(self):
        r = self.admin.get("/api/orders")
        self.assertEqual(r.status_code, 200, r.get_json())
        rows = [o for o in r.get_json()["orders"] if int(o["id"]) == self.internal_id]
        self.assertEqual(len(rows), 1)
        o = rows[0]
        self.assertEqual(o["source"], "reseller_api")
        self.assertEqual(o["public_order_id"], self.public_oid)
        self.assertEqual(o["reseller_public_id"], self.reseller["public_id"])
        self.assertEqual(o["reseller_name"], "P12M AdminView")  # name passed to _make_reseller
        self.assertEqual(o["client_order_id"], "admin-view-1")
        self.assertEqual(o["method_label"], "Reseller API")

    def test_admin_orders_source_filter(self):
        r = self.admin.get("/api/orders?source=reseller_api")
        ids = {int(o["id"]) for o in r.get_json()["orders"]}
        self.assertIn(self.internal_id, ids)
        r2 = self.admin.get("/api/orders?source=customer")
        ids2 = {int(o["id"]) for o in r2.get_json()["orders"]}
        self.assertNotIn(self.internal_id, ids2)

    def test_reseller_detail_counts_api_orders(self):
        r = self.admin.get(f"/api/resellers/{self.reseller['public_id']}")
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(d["orders_count"], 1)            # the API order
        self.assertEqual(d["customer_orders_count"], 0)   # no customer orders

    def test_resellers_list_counts_api_orders(self):
        r = self.admin.get("/api/resellers?per_page=100")
        self.assertEqual(r.status_code, 200)
        row = [x for x in r.get_json()["resellers"]
               if x["public_id"] == self.reseller["public_id"]]
        self.assertEqual(len(row), 1)
        self.assertGreaterEqual(row[0]["orders_count"], 1)

    def test_orders_csv_export_includes_reseller_linkage(self):
        r = self.admin.get("/api/export/orders")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("reseller_api", body)
        self.assertIn("admin-view-1", body)

    def test_unauthenticated_admin_api_401(self):
        anon = wa.app.test_client()
        r = anon.get("/api/orders")
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
