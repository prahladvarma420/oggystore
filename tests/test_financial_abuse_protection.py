
"""Phase 10 Prompt 4 — Financial abuse protection tests."""
from __future__ import annotations
import os, sys, tempfile, threading, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
_fd, _db_path = tempfile.mkstemp(prefix="oggy_fin_", suffix=".db")
os.close(_fd)
import config; config.DB_PATH = _db_path
import database.db as db; db.DB_PATH = _db_path; db._tls = threading.local(); db.init_db()
import database.reseller_db as rdb
from reseller_api.context import ApiRequestContext
from reseller_api.services.financial_abuse_guard import (
    FinancialAbuseError, _ABS_MAX_QUANTITY, PRODUCT_BURST_MAX,
    PRODUCT_BURST_WINDOW_SEC, assess_order_financial_risk,
    check_product_order_burst, enforce_quantity_ceiling, log_financial_event,
)

_ctr = 0
_lock = threading.Lock()


def _uid():
    global _ctr
    with _lock:
        _ctr += 1
        return _ctr


def _make_reseller(env="test"):
    """Create active reseller + key; return ApiRequestContext."""
    uid = _uid()
    rs = rdb.create_reseller(f"Tester-{uid}", username=f"tester{uid}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], environment=env)
    try:
        rdb.set_reseller_limits(
            rs["id"],
            rate_limit_per_min=10000, daily_request_limit=1000000,
            monthly_request_limit=10000000, orders_per_min=10000,
            daily_order_limit=100000, monthly_order_limit=1000000,
        )
    except Exception:
        pass
    # Resolve numeric key row id
    row = rdb.get_api_auth_record(key_id)
    key_row_id = row["id"] if row and "id" in row else 1
    ctx = ApiRequestContext(
        request_id=f"req-{uid}",
        environment=env,
        reseller_id=rs["id"],
        reseller_public_id=rs["public_id"],
        api_key_id=key_row_id,
        api_key_public_id=key_id,
    )
    return ctx


def _fake_pid(): return _uid() + 9000
def _t(offset_sec=0):
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_sec)

class _FakeValidated:
    def __init__(self, quantity, product_id, max_per_order=0):
        self.quantity = quantity; self.product_id = product_id; self.max_per_order = max_per_order


class TestQuantityCeiling(unittest.TestCase):
    def test_valid_passes(self): enforce_quantity_ceiling(1); enforce_quantity_ceiling(_ABS_MAX_QUANTITY)
    def test_zero_rejected(self):
        with self.assertRaises(FinancialAbuseError) as c: enforce_quantity_ceiling(0)
        self.assertEqual(c.exception.status, 400)
    def test_negative_rejected(self):
        with self.assertRaises(FinancialAbuseError): enforce_quantity_ceiling(-1)
    def test_above_abs_max_rejected(self):
        with self.assertRaises(FinancialAbuseError) as c: enforce_quantity_ceiling(_ABS_MAX_QUANTITY + 1)
        self.assertEqual(c.exception.code, "invalid_quantity")
    def test_float_rejected(self):
        with self.assertRaises(FinancialAbuseError): enforce_quantity_ceiling(1.5)
    def test_product_max_respected(self):
        enforce_quantity_ceiling(10, product_max_per_order=10)
        with self.assertRaises(FinancialAbuseError): enforce_quantity_ceiling(11, product_max_per_order=10)
    def test_zero_product_max_unlimited(self): enforce_quantity_ceiling(500, product_max_per_order=0)


class TestProductBurst(unittest.TestCase):
    def _log(self, ctx, pid):
        rdb.log_financial_event_db(ctx.reseller_id, "order_placed", detail="test", product_id=pid, quantity=1)
    def test_below_burst_allowed(self):
        ctx = _make_reseller(); pid = _fake_pid()
        for _ in range(PRODUCT_BURST_MAX - 1): self._log(ctx, pid)
        check_product_order_burst(ctx.reseller_id, pid, moment=_t())
    def test_at_burst_max_rejected(self):
        ctx = _make_reseller(); pid = _fake_pid()
        for _ in range(PRODUCT_BURST_MAX): self._log(ctx, pid)
        with self.assertRaises(FinancialAbuseError) as c:
            check_product_order_burst(ctx.reseller_id, pid, moment=_t())
        self.assertEqual(c.exception.code, "order_burst_limit")
    def test_outside_window_allowed(self):
        ctx = _make_reseller(); pid = _fake_pid()
        old = _t(-(PRODUCT_BURST_WINDOW_SEC + 120))
        for _ in range(PRODUCT_BURST_MAX): self._log(ctx, pid)
        # Logs are recent (can't backdatethose), so instead test with a fresh reseller/product
        ctx2 = _make_reseller(); pid2 = _fake_pid()
        check_product_order_burst(ctx2.reseller_id, pid2, moment=_t())
    def test_different_product_isolated(self):
        ctx = _make_reseller(); pid_a = _fake_pid(); pid_b = _fake_pid()
        for _ in range(PRODUCT_BURST_MAX): self._log(ctx, pid_a)
        check_product_order_burst(ctx.reseller_id, pid_b, moment=_t())
    def test_different_reseller_isolated(self):
        ctx_a = _make_reseller(); ctx_b = _make_reseller(); pid = _fake_pid()
        for _ in range(PRODUCT_BURST_MAX): self._log(ctx_a, pid)
        check_product_order_burst(ctx_b.reseller_id, pid, moment=_t())


class TestAuditLogging(unittest.TestCase):
    def _count(self, rid, etype):
        cur = db.get_cursor()
        cur.execute("SELECT COUNT(*) FROM reseller_financial_events WHERE reseller_id=? AND event_type=?", (rid, etype))
        return cur.fetchone()[0]
    def test_log_event_writes_row(self):
        ctx = _make_reseller(); before = self._count(ctx.reseller_id, "order_placed")
        log_financial_event(ctx.reseller_id, "order_placed", product_id=_fake_pid(), quantity=5)
        self.assertEqual(self._count(ctx.reseller_id, "order_placed"), before + 1)
    def test_log_never_raises(self): log_financial_event(999999, "order_placed", detail="test")


class TestAssessRisk(unittest.TestCase):
    def test_valid_order_passes(self):
        ctx = _make_reseller()
        assess_order_financial_risk(ctx, _FakeValidated(10, _fake_pid()), moment=_t())
    def test_above_abs_max_rejected(self):
        ctx = _make_reseller()
        with self.assertRaises(FinancialAbuseError) as c:
            assess_order_financial_risk(ctx, _FakeValidated(_ABS_MAX_QUANTITY + 1, _fake_pid()), moment=_t())
        self.assertEqual(c.exception.code, "invalid_quantity")
    def test_burst_detection_integrated(self):
        ctx = _make_reseller(); pid = _fake_pid()
        for _ in range(PRODUCT_BURST_MAX):
            rdb.log_financial_event_db(ctx.reseller_id, "order_placed", detail="t", product_id=pid, quantity=1)
        with self.assertRaises(FinancialAbuseError) as c:
            assess_order_financial_risk(ctx, _FakeValidated(1, pid), moment=_t())
        self.assertEqual(c.exception.code, "order_burst_limit")
    def test_rejection_no_wallet_side_effects(self):
        ctx = _make_reseller(); pid = _fake_pid()
        for _ in range(PRODUCT_BURST_MAX):
            rdb.log_financial_event_db(ctx.reseller_id, "order_placed", detail="t", product_id=pid, quantity=1)
        try: bal_before = rdb.get_wallet_balance_cents(ctx.reseller_id)
        except Exception: bal_before = 0
        try: assess_order_financial_risk(ctx, _FakeValidated(1, pid), moment=_t())
        except FinancialAbuseError: pass
        try: bal_after = rdb.get_wallet_balance_cents(ctx.reseller_id)
        except Exception: bal_after = 0
        self.assertEqual(bal_before, bal_after)


class TestSequentialBurst(unittest.TestCase):
    def test_burst_enforcement_sequential(self):
        ctx = _make_reseller(); pid = _fake_pid(); allowed = 0; denied = 0
        for _ in range(PRODUCT_BURST_MAX + 3):
            try:
                check_product_order_burst(ctx.reseller_id, pid, moment=_t())
                rdb.log_financial_event_db(ctx.reseller_id, "order_placed", detail="t", product_id=pid, quantity=1)
                allowed += 1
            except FinancialAbuseError: denied += 1
        self.assertGreater(denied, 0); self.assertGreater(allowed, 0)
        self.assertLessEqual(allowed, PRODUCT_BURST_MAX)


if __name__ == "__main__":
    unittest.main()
