
"""Phase 10 Prompt 7 - Reseller quota & usage limit tests."""
from __future__ import annotations
import os, sys, tempfile, threading, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
_fd, _db_path = tempfile.mkstemp(prefix="oggy_quota_", suffix=".db")
os.close(_fd)
import config; config.DB_PATH = _db_path
import database.db as db; db.DB_PATH = _db_path; db._tls = threading.local(); db.init_db()
import database.reseller_db as rdb
from reseller_api.context import ApiRequestContext
from reseller_api.services.quota_service import QuotaExceededError, check_order_quota, record_order_usage

_ctr = 0; _lock = threading.Lock()
def _uid():
    global _ctr
    with _lock: _ctr += 1; return _ctr

def _make_reseller():
    uid = _uid()
    rs = rdb.create_reseller(f"QuotaTester-{uid}", username=f"quota{uid}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], environment="test")
    row = rdb.get_api_auth_record(key_id)
    # row uses key "api_key_id" for the integer DB row id
    key_row_id = row["api_key_id"] if row and "api_key_id" in row else 1
    return ApiRequestContext(
        request_id=f"req-{uid}", environment="test",
        reseller_id=rs["id"], reseller_public_id=rs["public_id"],
        api_key_id=key_row_id, api_key_public_id=key_id,
    )

def _log_n(ctx, n):
    for _ in range(n): record_order_usage(ctx, product_id=_uid()+5000, quantity=1)


class TestQuotaUnlimited(unittest.TestCase):
    def test_default_is_unlimited(self): check_order_quota(_make_reseller())
    def test_explicit_zero_unlimited(self):
        ctx = _make_reseller()
        rdb.set_quota_settings(ctx.reseller_id, daily_order_quota=0, monthly_order_quota=0)
        _log_n(ctx, 100); check_order_quota(ctx)


class TestDailyQuota(unittest.TestCase):
    def test_below_daily_allowed(self):
        ctx = _make_reseller(); rdb.set_quota_settings(ctx.reseller_id, daily_order_quota=10)
        _log_n(ctx, 5); check_order_quota(ctx)
    def test_at_daily_rejected(self):
        ctx = _make_reseller(); rdb.set_quota_settings(ctx.reseller_id, daily_order_quota=5)
        _log_n(ctx, 5)
        with self.assertRaises(QuotaExceededError) as c: check_order_quota(ctx)
        self.assertEqual(c.exception.code, "daily_order_quota_exceeded")
    def test_above_daily_rejected(self):
        ctx = _make_reseller(); rdb.set_quota_settings(ctx.reseller_id, daily_order_quota=3)
        _log_n(ctx, 10)
        with self.assertRaises(QuotaExceededError): check_order_quota(ctx)


class TestMonthlyQuota(unittest.TestCase):
    def test_below_monthly_allowed(self):
        ctx = _make_reseller(); rdb.set_quota_settings(ctx.reseller_id, monthly_order_quota=100)
        _log_n(ctx, 50); check_order_quota(ctx)
    def test_at_monthly_rejected(self):
        ctx = _make_reseller(); rdb.set_quota_settings(ctx.reseller_id, monthly_order_quota=10)
        _log_n(ctx, 10)
        with self.assertRaises(QuotaExceededError) as c: check_order_quota(ctx)
        self.assertEqual(c.exception.code, "monthly_order_quota_exceeded")
    def test_daily_and_monthly_independent(self):
        ctx = _make_reseller()
        rdb.set_quota_settings(ctx.reseller_id, daily_order_quota=100, monthly_order_quota=3)
        _log_n(ctx, 3)
        with self.assertRaises(QuotaExceededError) as c: check_order_quota(ctx)
        self.assertEqual(c.exception.code, "monthly_order_quota_exceeded")


class TestApiEnabled(unittest.TestCase):
    def test_api_disabled_raises_403(self):
        ctx = _make_reseller(); rdb.set_quota_settings(ctx.reseller_id, api_enabled=False)
        with self.assertRaises(QuotaExceededError) as c: check_order_quota(ctx)
        self.assertEqual(c.exception.code, "api_disabled"); self.assertEqual(c.exception.status, 403)
    def test_api_enabled_allows(self):
        ctx = _make_reseller(); rdb.set_quota_settings(ctx.reseller_id, api_enabled=True)
        check_order_quota(ctx)


class TestResellerIsolation(unittest.TestCase):
    def test_isolated(self):
        ctx_a = _make_reseller(); ctx_b = _make_reseller()
        rdb.set_quota_settings(ctx_a.reseller_id, daily_order_quota=5)
        rdb.set_quota_settings(ctx_b.reseller_id, daily_order_quota=5)
        _log_n(ctx_a, 5)
        with self.assertRaises(QuotaExceededError): check_order_quota(ctx_a)
        check_order_quota(ctx_b)


class TestAdminCanChangeQuota(unittest.TestCase):
    def test_quota_increase_effective(self):
        ctx = _make_reseller(); rdb.set_quota_settings(ctx.reseller_id, daily_order_quota=3)
        _log_n(ctx, 3)
        with self.assertRaises(QuotaExceededError): check_order_quota(ctx)
        rdb.set_quota_settings(ctx.reseller_id, daily_order_quota=10)
        check_order_quota(ctx)
    def test_quota_decrease_effective(self):
        ctx = _make_reseller(); rdb.set_quota_settings(ctx.reseller_id, daily_order_quota=100)
        _log_n(ctx, 5); rdb.set_quota_settings(ctx.reseller_id, daily_order_quota=3)
        with self.assertRaises(QuotaExceededError): check_order_quota(ctx)


class TestRecordOrderUsage(unittest.TestCase):
    def test_record_never_raises(self): record_order_usage(_make_reseller(), product_id=999, quantity=5)
    def test_record_increments_count(self):
        ctx = _make_reseller(); rdb.set_quota_settings(ctx.reseller_id, daily_order_quota=3)
        _log_n(ctx, 3)
        with self.assertRaises(QuotaExceededError): check_order_quota(ctx)


if __name__ == "__main__":
    unittest.main()
