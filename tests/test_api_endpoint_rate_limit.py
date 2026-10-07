
"""Phase 10 Prompt 3 — Endpoint-specific rate limiting tests.

Verifies:
  1. Each category (READ, DELIVERY_READ, ORDER_CREATE, WEBHOOK_WRITE,
     WEBHOOK_RETRY) enforces its own independent counter.
  2. Normal traffic within limits is allowed.
  3. Requests above the limit return denied=True.
  4. Cross-category isolation: exhausting READ does not consume ORDER quota.
  5. Different resellers are isolated from each other.
  6. Bucket rollover resets the counter.
  7. DB error / bad category causes fail-open.
"""

from __future__ import annotations
import os, sys, tempfile, threading, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_fd, _db_path = tempfile.mkstemp(prefix="oggy_ep_rate_", suffix=".db")
os.close(_fd)
import config; config.DB_PATH = _db_path
import database.db as db; db.DB_PATH = _db_path; db._tls = threading.local(); db.init_db()
import database.reseller_db as rdb
from reseller_api.context import ApiRequestContext
from reseller_api.services.endpoint_rate_limit import (
    CATEGORY_READ, CATEGORY_DELIVERY_READ, CATEGORY_ORDER_CREATE,
    CATEGORY_WEBHOOK_WRITE, CATEGORY_WEBHOOK_RETRY,
    DEFAULT_LIMITS, EndpointRateDecision, check_endpoint_rate,
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


def _t(minute_offset=0):
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(minutes=minute_offset)


class TestCategoryLimits(unittest.TestCase):
    def test_read_limit_is_high(self):        self.assertGreaterEqual(DEFAULT_LIMITS[CATEGORY_READ], 100)
    def test_order_limit_lower_than_read(self): self.assertLess(DEFAULT_LIMITS[CATEGORY_ORDER_CREATE], DEFAULT_LIMITS[CATEGORY_READ])
    def test_webhook_retry_is_strictest(self):  self.assertLessEqual(DEFAULT_LIMITS[CATEGORY_WEBHOOK_RETRY], DEFAULT_LIMITS[CATEGORY_ORDER_CREATE])
    def test_all_limits_positive(self):
        for cat, lim in DEFAULT_LIMITS.items():
            with self.subTest(category=cat): self.assertGreater(lim, 0)


class TestNormalTrafficAllowed(unittest.TestCase):
    def test_read_first_request_allowed(self):
        d = check_endpoint_rate(_make_reseller(), CATEGORY_READ, limit=10, moment=_t())
        self.assertTrue(d.allowed)
    def test_order_first_request_allowed(self):
        d = check_endpoint_rate(_make_reseller(), CATEGORY_ORDER_CREATE, limit=5, moment=_t())
        self.assertTrue(d.allowed)
    def test_webhook_retry_first_request_allowed(self):
        d = check_endpoint_rate(_make_reseller(), CATEGORY_WEBHOOK_RETRY, limit=3, moment=_t())
        self.assertTrue(d.allowed)
    def test_returns_decision_object(self):
        d = check_endpoint_rate(_make_reseller(), CATEGORY_READ, limit=100, moment=_t())
        self.assertIsInstance(d, EndpointRateDecision)
        self.assertEqual(d.category, CATEGORY_READ)


class TestLimitEnforcement(unittest.TestCase):
    def test_read_limit_reached(self):
        ctx = _make_reseller(); limit = 5; t = _t()
        for _ in range(limit): check_endpoint_rate(ctx, CATEGORY_READ, limit=limit, moment=t)
        d = check_endpoint_rate(ctx, CATEGORY_READ, limit=limit, moment=t)
        self.assertFalse(d.allowed); self.assertEqual(d.reason, "rate_limit_exceeded")
    def test_order_create_limit_reached(self):
        ctx = _make_reseller(); limit = 3; t = _t()
        for _ in range(limit): check_endpoint_rate(ctx, CATEGORY_ORDER_CREATE, limit=limit, moment=t)
        self.assertFalse(check_endpoint_rate(ctx, CATEGORY_ORDER_CREATE, limit=limit, moment=t).allowed)
    def test_webhook_retry_limit_reached(self):
        ctx = _make_reseller(); limit = 2; t = _t()
        for _ in range(limit): check_endpoint_rate(ctx, CATEGORY_WEBHOOK_RETRY, limit=limit, moment=t)
        self.assertFalse(check_endpoint_rate(ctx, CATEGORY_WEBHOOK_RETRY, limit=limit, moment=t).allowed)
    def test_remaining_decrements(self):
        ctx = _make_reseller(); limit = 5; t = _t()
        d1 = check_endpoint_rate(ctx, CATEGORY_READ, limit=limit, moment=t)
        d2 = check_endpoint_rate(ctx, CATEGORY_READ, limit=limit, moment=t)
        self.assertGreater(d1.remaining, d2.remaining)
    def test_denied_includes_retry_after(self):
        ctx = _make_reseller(); t = _t()
        check_endpoint_rate(ctx, CATEGORY_READ, limit=1, moment=t)
        d = check_endpoint_rate(ctx, CATEGORY_READ, limit=1, moment=t)
        self.assertIn("Retry-After", d.headers(include_retry=True))


class TestCrossCategoryIsolation(unittest.TestCase):
    def test_read_exhaustion_does_not_block_order(self):
        ctx = _make_reseller(); t = _t()
        for _ in range(5): check_endpoint_rate(ctx, CATEGORY_READ, limit=5, moment=t)
        check_endpoint_rate(ctx, CATEGORY_READ, limit=5, moment=t)  # denied
        self.assertTrue(check_endpoint_rate(ctx, CATEGORY_ORDER_CREATE, limit=5, moment=t).allowed)
    def test_order_exhaustion_does_not_block_read(self):
        ctx = _make_reseller(); t = _t()
        for _ in range(3): check_endpoint_rate(ctx, CATEGORY_ORDER_CREATE, limit=3, moment=t)
        check_endpoint_rate(ctx, CATEGORY_ORDER_CREATE, limit=3, moment=t)  # denied
        self.assertTrue(check_endpoint_rate(ctx, CATEGORY_READ, limit=20, moment=t).allowed)
    def test_webhook_retry_isolated_from_webhook_write(self):
        ctx = _make_reseller(); t = _t()
        for _ in range(2): check_endpoint_rate(ctx, CATEGORY_WEBHOOK_RETRY, limit=2, moment=t)
        check_endpoint_rate(ctx, CATEGORY_WEBHOOK_RETRY, limit=2, moment=t)  # denied
        self.assertTrue(check_endpoint_rate(ctx, CATEGORY_WEBHOOK_WRITE, limit=10, moment=t).allowed)


class TestResellerIsolation(unittest.TestCase):
    def test_different_resellers_independent(self):
        ctx_a = _make_reseller(); ctx_b = _make_reseller(); t = _t(); limit = 3
        for _ in range(limit): check_endpoint_rate(ctx_a, CATEGORY_READ, limit=limit, moment=t)
        self.assertFalse(check_endpoint_rate(ctx_a, CATEGORY_READ, limit=limit, moment=t).allowed)
        self.assertTrue(check_endpoint_rate(ctx_b, CATEGORY_READ, limit=limit, moment=t).allowed)


class TestBucketRollover(unittest.TestCase):
    def test_counter_resets_next_minute(self):
        ctx = _make_reseller(); limit = 2
        for _ in range(limit): check_endpoint_rate(ctx, CATEGORY_ORDER_CREATE, limit=limit, moment=_t(0))
        self.assertFalse(check_endpoint_rate(ctx, CATEGORY_ORDER_CREATE, limit=limit, moment=_t(0)).allowed)
        self.assertTrue(check_endpoint_rate(ctx, CATEGORY_ORDER_CREATE, limit=limit, moment=_t(1)).allowed)


class TestFailOpen(unittest.TestCase):
    def test_bad_category_fails_open(self):
        self.assertTrue(check_endpoint_rate(_make_reseller(), "__invalid__", limit=5, moment=_t()).allowed)
    def test_zero_limit_fails_open(self):
        self.assertTrue(check_endpoint_rate(_make_reseller(), CATEGORY_READ, limit=0, moment=_t()).allowed)


class TestHeaderOutput(unittest.TestCase):
    def test_allowed_headers_present(self):
        d = check_endpoint_rate(_make_reseller(), CATEGORY_READ, limit=10, moment=_t())
        h = d.headers()
        self.assertIn("RateLimit-Limit", h); self.assertIn("RateLimit-Remaining", h)
        self.assertNotIn("Retry-After", h)
    def test_denied_headers_include_retry(self):
        ctx = _make_reseller(); t = _t()
        check_endpoint_rate(ctx, CATEGORY_WEBHOOK_RETRY, limit=1, moment=t)
        d = check_endpoint_rate(ctx, CATEGORY_WEBHOOK_RETRY, limit=1, moment=t)
        self.assertIn("Retry-After", d.headers(include_retry=True))


if __name__ == "__main__":
    unittest.main()
