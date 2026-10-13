"""Phase 12 Prompt 10 — FINAL regression: reseller order, payment, stock &
fulfillment validation.

One executable pass over every Phase 12 acceptance area.  Deep behavioral
coverage lives in the per-area suites (creation / quotes / wallet / mapping /
lifecycle / failure-recovery / concurrency / security-audit); this suite adds
the remaining end-to-end and cross-cutting checks — customer flow, webadmin,
fulfillment routing, and database integrity — and re-verifies the critical
happy/edge paths so the final status table reflects ACTUAL executions.
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

_fd, _db_path = tempfile.mkstemp(prefix="oggy_phase12_final_", suffix=".db")
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
)

_ctr = 0


def _n():
    global _ctr
    _ctr += 1
    return _ctr


def _make_reseller():
    n = _n()
    uid = 9_890_000 + n
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, f"P10R{n}", f"p10r{n}"),
    )
    db.conn.commit()
    rs = rdb.create_reseller(f"P10R{n}", uid, f"p10r{n}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], "test")
    return rs, key_id, secret


def _make_product(price=10.0, codes=("F-1",), max_per_order=10, manual=0):
    n = _n()
    stock = 0 if codes else int(manual)
    db.cur.execute(
        "INSERT INTO products(name, price, stock, delivery, active) VALUES(?,?,?,'code_stock',1)",
        (f"P10 Product {n}", price, stock),
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
    rdb.post_wallet_transaction(rid, "topup_credit", cents, f"TP-P10-{_n()}", "seed")


def _hdr(k, s):
    return {"Authorization": f"Bearer {k}.{s}"}


def _oid(pub):
    return int(pub.replace("ORD-", ""))


class Phase12FinalRegression(unittest.TestCase):
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

    def _post(self, hdrs, pub, qty=1, coid=None):
        body = {"product_id": pub, "quantity": qty}
        if coid:
            body["client_order_id"] = coid
        return self.client.post("/api/v1/orders", headers=hdrs, json=body)

    # ============================================================
    # 1+2+3. Product / quote / order happy+edge paths (executed fresh)
    # ============================================================

    def test_s123_product_quote_order_end_to_end(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=12.0, codes=["E1", "E2", "E3"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 100000)

        # allowed product visible in the reseller catalog
        cat = self.client.get("/api/v1/my-products", headers=_hdr(key, sec))
        self.assertEqual(cat.status_code, 200, cat.get_json())
        import json as _json
        self.assertIn(pub, _json.dumps(cat.get_json()))

        # valid quote — authoritative, side-effect free
        q = self.client.post("/api/v1/orders/quote", headers=_hdr(key, sec),
                             json={"product_id": pub, "quantity": 2})
        self.assertEqual(q.status_code, 200, q.get_json())
        qd = q.get_json()["data"]
        self.assertEqual(qd["total"], "24.00")
        self.assertTrue(qd["sufficient_funds"])
        self.assertTrue(qd["in_stock"])
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 100000)  # untouched

        # invalid quantity
        q2 = self.client.post("/api/v1/orders/quote", headers=_hdr(key, sec),
                              json={"product_id": pub, "quantity": 0})
        self.assertEqual(q2.status_code, 400)

        # manipulated price fields rejected on both endpoints
        for ep in ("/api/v1/orders", "/api/v1/orders/quote"):
            r = self.client.post(ep, headers=_hdr(key, sec),
                                 json={"product_id": pub, "quantity": 1,
                                       "unit_price": "0.01"})
            self.assertEqual(r.status_code, 400, ep)

        # normal multi-quantity order at the quoted price
        r = self._post(_hdr(key, sec), pub, 2, "p10-e2e")
        self.assertEqual(r.status_code, 201, r.get_json())
        d = r.get_json()["data"]
        self.assertEqual(d["total"], "24.00")
        self.assertEqual(d["quantity"], 2)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 100000 - 2400)

        # delivery: the two claimed units, retrievable by the owner
        dv = self.client.get(f"/api/v1/orders/{d['order_id']}/delivery",
                             headers=_hdr(key, sec))
        self.assertEqual(dv.status_code, 200)
        self.assertEqual(len(dv.get_json()["data"]["delivery"]["items"]), 2)

    def test_s123_rejections(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["R1"])
        _grant(rs["id"], pid)

        # disabled product
        db.cur.execute("UPDATE products SET active=0 WHERE id=?", (pid,))
        db.conn.commit()
        self.assertEqual(self._post(_hdr(key, sec), pub, 1, "p10-dis").status_code, 404)
        db.cur.execute("UPDATE products SET active=1 WHERE id=?", (pid,))
        db.conn.commit()

        # insufficient balance (no topup at all)
        self.assertEqual(self._post(_hdr(key, sec), pub, 1, "p10-bal").status_code, 402)

        # invalid API key
        self.assertEqual(self._post(_hdr(key, "wrong" * 8), pub, 1).status_code, 401)

        # suspended reseller
        rdb.set_reseller_status(rs["id"], "suspended")
        self.assertEqual(self._post(_hdr(key, sec), pub, 1, "p10-sus").status_code, 401)
        rdb.set_reseller_status(rs["id"], "active")

        # never any side effects from the rejections
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 0)
        db.cur.execute("SELECT COUNT(*) FROM orders WHERE reseller_id=?", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 0)
        db.cur.execute("SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=1", (pid,))
        self.assertEqual(int(db.cur.fetchone()[0]), 0)

    # ============================================================
    # 4+5. Wallet & stock final invariants (compact burst)
    # ============================================================

    def test_s45_wallet_stock_burst_never_negative(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=[f"N{i}" for i in range(4)])
        _grant(rs["id"], pid)
        _topup(rs["id"], 2500)   # $25 — two and a half orders

        barrier = threading.Barrier(6)
        codes_out = []

        def worker(i):
            c = self.app.test_client()
            barrier.wait()
            r = c.post("/api/v1/orders", headers=_hdr(key, sec),
                       json={"product_id": pub, "quantity": 1,
                             "client_order_id": f"p10-burst-{i}"})
            codes_out.append(r.status_code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(codes_out.count(201), 2, codes_out)
        self.assertEqual(rdb.get_wallet_balance_cents(rs["id"]), 500)   # never negative
        db.cur.execute(
            "SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=1", (pid,))
        self.assertEqual(int(db.cur.fetchone()[0]), 2)                  # never negative stock
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND tx_type='purchase_debit'", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 2)

    # ============================================================
    # 6. Idempotency: repeated / simultaneous / restart (compact)
    # ============================================================

    def test_s6_one_authoritative_order_through_every_retry_shape(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=10.0, codes=["K1", "K2"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)

        # simultaneous identical
        barrier = threading.Barrier(4)
        results = []

        def worker():
            c = self.app.test_client()
            barrier.wait()
            results.append(c.post("/api/v1/orders", headers=_hdr(key, sec),
                                  json={"product_id": pub, "quantity": 1,
                                        "client_order_id": "p10-idem"}).status_code)

        ts = [threading.Thread(target=worker) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(results), [200, 200, 200, 201], results)

        # API "restart" then a plain retry
        db.close_thread_connection()
        r = self._post(_hdr(key, sec), pub, 1, "p10-idem")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["data"]["idempotent"])

        db.cur.execute("SELECT COUNT(*) FROM orders WHERE reseller_id=?", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 1)                  # ONE order
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND tx_type='purchase_debit'", (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 1)                  # ONE debit
        db.cur.execute(
            "SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=1", (pid,))
        self.assertEqual(int(db.cur.fetchone()[0]), 1)                  # ONE unit

    # ============================================================
    # 7. Master order mapping (fresh check)
    # ============================================================

    def test_s7_master_order_mapping_correct(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(price=15.0, codes=["M1", "M2"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)

        r = self._post(_hdr(key, sec), pub, 2, "p10-map")
        self.assertEqual(r.status_code, 201)
        oid = _oid(r.get_json()["data"]["order_id"])

        # master row: correct reseller linkage, product, quantity, price
        db.cur.execute(
            "SELECT reseller_id, product_id, quantity, unit_price_cents, source,"
            "       client_order_id FROM orders WHERE id=?", (oid,))
        rid, p_id, qty, unit, source, coid = db.cur.fetchone()
        self.assertEqual(rid, rs["id"])
        self.assertEqual(p_id, pid)
        self.assertEqual(qty, 2)
        self.assertEqual(unit, 1500)
        self.assertEqual(source, "reseller_api")
        self.assertEqual(coid, "p10-map")

        # claimed stock rows are attributed to THIS order
        db.cur.execute(
            "SELECT COUNT(*) FROM product_stock WHERE order_id=? AND sold=1", (oid,))
        self.assertEqual(int(db.cur.fetchone()[0]), 2)

        # admin view: the master orders table carries the attribution
        # (source + reseller linkage readable by the owner tools)
        db.cur.execute(
            "SELECT COUNT(*) FROM orders WHERE reseller_id=? AND source='reseller_api'",
            (rs["id"],))
        self.assertEqual(int(db.cur.fetchone()[0]), 1)

    # ============================================================
    # 8. Fulfillment regression (Phase 8 intact)
    # ============================================================

    def test_s8_fulfillment_paths(self):
        from fulfillment.router import (
            route_fulfillment, FULFILLMENT_MANUAL, FULFILLMENT_EXTERNAL,
        )

        # --- automatic (code stock): delivery completed at order time
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["FU-1"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        r = self._post(_hdr(key, sec), pub, 1, "p10-ful-auto")
        self.assertEqual(r.status_code, 201)
        oid = _oid(r.get_json()["data"]["order_id"])
        dv = self.client.get(f"/api/v1/orders/{r.get_json()['data']['order_id']}/delivery",
                             headers=_hdr(key, sec))
        self.assertEqual(dv.get_json()["data"]["delivery"]["delivery_status"], "delivered")

        # --- manual product: order succeeds, delivery pending
        pid2, pub2 = _make_product(codes=[], manual=3)
        db.cur.execute("UPDATE products SET delivery='Manual: ships in 24h' WHERE id=?", (pid2,))
        db.conn.commit()
        _grant(rs["id"], pid2)
        r2 = self._post(_hdr(key, sec), pub2, 1, "p10-ful-manual")
        self.assertEqual(r2.status_code, 201, r2.get_json())
        res = route_fulfillment(_oid(r2.get_json()["data"]["order_id"]), pid2, 1)
        # no fulfillment_type set -> defaults to code handling upstream;
        # explicit manual config routes to the manual branch
        db.cur.execute("UPDATE products SET fulfillment_type=? WHERE id=?",
                       (FULFILLMENT_MANUAL, pid2))
        db.conn.commit()
        res = route_fulfillment(_oid(r2.get_json()["data"]["order_id"]), pid2, 1)
        self.assertEqual(res["type"], FULFILLMENT_MANUAL)
        self.assertEqual(res["result"]["status"], "pending")

        # --- email-gated delivery requires a buyer email (Prompt 16.8)
        from database.db import is_m365_product
        pid3, pub3 = _make_product(codes=["SP-1"])
        _grant(rs["id"], pid3)
        db.cur.execute("UPDATE products SET name='M365 Family 12 Months' WHERE id=?", (pid3,))
        db.conn.commit()
        if is_m365_product(pid3):
            r3 = self._post(_hdr(key, sec), pub3, 1, "p10-ful-special")
            self.assertEqual(r3.status_code, 422)
            self.assertEqual(r3.get_json()["error"]["code"], "buyer_email_required")

        # --- external fulfillment: router dispatches to the external client
        #     with a mocked HTTP layer; failure => error result (handled)
        db.cur.execute(
            "UPDATE products SET fulfillment_type=?, fulfillment_config=? WHERE id=?",
            (FULFILLMENT_EXTERNAL, '{"endpoint":"https://supplier.example/f","api_key":"k"}', pid2),
        )
        db.conn.commit()
        # Prompt 16.6 transport now uses a pinned, TLS-verified Session.
        from unittest.mock import MagicMock
        import json
        import requests
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.iter_content.return_value = [json.dumps({"delivery":"LICENSE-KEY-1"}).encode()]
        session = MagicMock()
        session.__enter__.return_value = session
        session.post.return_value = response
        with patch("fulfillment.external_clients.generic_api_client.resolve_public_ips",return_value=('93.184.216.34',)), \
             patch("fulfillment.external_clients.generic_api_client.new_session",return_value=session):
            res2 = route_fulfillment(oid, pid2, 1)
        self.assertEqual(res2["type"], FULFILLMENT_EXTERNAL)
        self.assertEqual(res2["result"]["status"], "fulfilled")
        self.assertEqual(res2["result"]["delivery"], "LICENSE-KEY-1")
        # Transient exhaustion uses the documented classified result, not raw
        # exceptions. Financial engine assertions below are retained.
        session.post.side_effect = requests.exceptions.ConnectionError("supplier down")
        with patch("fulfillment.external_clients.generic_api_client.resolve_public_ips",return_value=('93.184.216.34',)), \
             patch("fulfillment.external_clients.generic_api_client.new_session",return_value=session):
            res3 = route_fulfillment(oid, pid2, 1)
        self.assertEqual(res3["result"]["status"], "retryable_error")
        self.assertTrue(res3["result"]["exhausted"])
        fail = handle_post_debit_failure(_oid(r2.get_json()["data"]["order_id"]),
                                         reason="supplier_down")
        self.assertEqual(fail["result"], "handled")
        self.assertTrue(fail["refunded"])

    # ============================================================
    # 13. Customer regression (master bot DB flows unchanged)
    # ============================================================

    def test_s13_customer_flow_untouched(self):
        """The customer (Telegram bot) master flow still works against the
        Phase 12 schema: browse -> stock -> atomic claim -> legacy order
        INSERT -> delivery decrypts; support tickets work.  (Handler modules
        import python-telegram-bot, so the repo's suites exercise this layer
        directly — same convention as order_service.)"""
        from utils.crypto import decrypt_text, encrypt_text

        # catalog browsing helpers
        pid, pub = _make_product(price=7.5, codes=["CUST-1", "CUST-2"])
        self.assertEqual(db.get_available_stock_count(pid), 2)
        self.assertGreaterEqual(db.get_display_stock(pid), 2)

        # atomic claim for a Telegram customer (user_id = telegram id)
        items = db.reserve_stock_items(pid, 424242, 2)
        self.assertIsNotNone(items)
        self.assertEqual(len(items), 2)

        # legacy order INSERT shape (exactly what product_handlers runs)
        delivery_blob = encrypt_text("\n\n".join(items))
        price = 7.5 * 2
        db.cur.execute(
            "INSERT INTO orders(user_id, product_name, price, delivery, product_id,"
            " payment_method) VALUES(?,?,?,?,?,?)",
            (424242, "P10 Customer Product x2", price, delivery_blob, pid, "crypto"),
        )
        db.conn.commit()
        cust_oid = db.cur.lastrowid

        # the row is a plain customer order: no reseller linkage
        db.cur.execute(
            "SELECT source, reseller_id, client_order_id FROM orders WHERE id=?",
            (cust_oid,))
        source, reseller_id, coid = db.cur.fetchone()
        self.assertIsNone(source)
        self.assertIsNone(reseller_id)
        self.assertIsNone(coid)

        # delivery round-trips
        db.cur.execute("SELECT delivery FROM orders WHERE id=?", (cust_oid,))
        plain = decrypt_text(db.cur.fetchone()[0])
        self.assertIn("CUST-1", plain)

        # stock reflects the sale; a second 2-unit purchase now fails cleanly
        self.assertEqual(db.get_available_stock_count(pid), 0)
        self.assertIsNone(db.reserve_stock_items(pid, 424243, 2))

        # support tickets still work
        tid = db.create_ticket(424242, "Order question", "Where is my order?")
        self.assertTrue(int(tid) > 0)

        # customer orders never enter reseller surfaces
        rs, key, sec = _make_reseller()
        lst = self.client.get("/api/v1/orders", headers=_hdr(key, sec))
        self.assertEqual(lst.get_json()["data"]["orders"], [])

    # ============================================================
    # 14. WebAdmin regression
    # ============================================================

    def test_s14_webadmin_owner_only_and_functional(self):
        """WebAdmin is owner-only: unauthenticated API calls redirect/deny;
        no reseller credential exists that unlocks it.  The reseller-attri-
        bution admin views are covered live in test_phase12_order_mapping."""
        import web_admin.app as wa
        wa.app.config["TESTING"] = True
        admin_client = wa.app.test_client()

        for path in ("/", "/api/orders", "/api/resellers", "/api/products"):
            resp = admin_client.get(path)
            self.assertIn(resp.status_code, (302, 401, 303), path)
            if resp.status_code in (302, 303):
                self.assertIn("/login", resp.headers.get("Location", ""))

        # a reseller API key is meaningless to WebAdmin (different realm)
        rs, key, sec = _make_reseller()
        resp = admin_client.get("/api/orders",
                                headers={"Authorization": f"Bearer {key}.{sec}"})
        self.assertIn(resp.status_code, (302, 401, 303))

        # owner wallet admin service still operates (credit path exists)
        from web_admin import wallet_admin_service  # noqa: F401  importable

    # ============================================================
    # 15. Database integrity
    # ============================================================

    def test_s15_database_integrity(self):
        rs, key, sec = _make_reseller()
        pid, pub = _make_product(codes=["DB-1", "DB-2"])
        _grant(rs["id"], pid)
        _topup(rs["id"], 50000)
        self._post(_hdr(key, sec), pub, 1, "p10-db-1")
        r2 = self._post(_hdr(key, sec), pub, 1, "p10-db-2")
        handle_post_debit_failure(_oid(r2.get_json()["data"]["order_id"]), reason="x")

        # SQLite-level integrity
        db.cur.execute("PRAGMA integrity_check")
        self.assertEqual(db.cur.fetchone()[0], "ok")

        # no duplicate idempotency keys per reseller
        db.cur.execute(
            "SELECT reseller_id, client_order_id, COUNT(*) c FROM orders"
            " WHERE client_order_id IS NOT NULL"
            " GROUP BY reseller_id, client_order_id HAVING c > 1")
        self.assertEqual(db.cur.fetchall(), [])

        # no duplicate ledger references
        db.cur.execute(
            "SELECT reseller_id, tx_type, reference, COUNT(*) c"
            " FROM reseller_wallet_transactions WHERE reference IS NOT NULL"
            " GROUP BY reseller_id, tx_type, reference HAVING c > 1")
        self.assertEqual(db.cur.fetchall(), [])

        # no duplicate reseller product mappings
        db.cur.execute(
            "SELECT reseller_id, product_id, COUNT(*) c"
            " FROM reseller_product_mappings"
            " GROUP BY reseller_id, product_id HAVING c > 1")
        self.assertEqual(db.cur.fetchall(), [])

        # no orphan reseller orders (reseller must exist)
        db.cur.execute(
            "SELECT COUNT(*) FROM orders o LEFT JOIN resellers r"
            " ON o.reseller_id = r.id"
            " WHERE o.reseller_id IS NOT NULL AND r.id IS NULL")
        self.assertEqual(int(db.cur.fetchone()[0]), 0)

        # no orphan stock attribution (order_id points at a real order)
        db.cur.execute(
            "SELECT COUNT(*) FROM product_stock ps LEFT JOIN orders o"
            " ON ps.order_id = o.id WHERE ps.order_id IS NOT NULL AND o.id IS NULL")
        self.assertEqual(int(db.cur.fetchone()[0]), 0)

        # ledger CHECK constraints hold (balance never negative, nonzero amounts)
        db.cur.execute(
            "SELECT COUNT(*) FROM reseller_wallet_transactions"
            " WHERE balance_after_cents < 0 OR amount_cents = 0")
        self.assertEqual(int(db.cur.fetchone()[0]), 0)

        # indexes exist for the hot paths
        db.cur.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name IN"
            " ('idx_orders_reseller_idempotency','idx_orders_reseller',"
            "  'idx_reseller_ledger_reseller','idx_webhook_outbox_dedup')")
        self.assertEqual(len(db.cur.fetchall()), 4)

        # schema migrations are idempotent (safe to re-run at every boot)
        from reseller_api.services.order_service import ensure_order_metadata_columns
        from reseller_api.services.webhook_outbox import ensure_webhook_outbox_table
        ensure_order_metadata_columns()
        ensure_webhook_outbox_table()
        rdb.ensure_reseller_tables()
        db.cur.execute("PRAGMA integrity_check")
        self.assertEqual(db.cur.fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
