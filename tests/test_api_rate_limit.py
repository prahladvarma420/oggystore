"""Phase 10 Prompt 2 — Global Reseller API Rate Limiting tests.

Covers:
  1.  IP auth-failure rate limiter (brute-force protection)
  2.  Global per-reseller request-rate limiter (minute/day/month)
  3.  Reseller quota isolation (cross-reseller independence)
  4.  Multiple API keys share one reseller quota
  5.  Key scenarios: revoked key, invalid key, bad secret
  6.  Kill-switch process-local TTL cache
  7.  Distributed safety (simulate multiple processes via threads)

All tests run against a real SQLite DB — no mocks, no Flask.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Isolated temp DB setup — same pattern as existing test suites
# ---------------------------------------------------------------------------

_fd, _db_path = tempfile.mkstemp(prefix="oggy_rate_limit_", suffix=".db")
os.close(_fd)

import config
config.DB_PATH = _db_path

import database.db as db
db.DB_PATH = _db_path
db._tls = threading.local()
db.init_db()

import database.reseller_db as rdb
from reseller_api.context import ApiRequestContext
from reseller_api.services.authentication import authenticate_credentials
from reseller_api.services.authorization import (
    FOUNDATION_READ,
    ORDERS_CREATE,
    PRODUCTS_READ,
    authorize_operation,
)
from reseller_api.services.rate_limit import (
    RequestRateDecision,
    evaluate_request_rate,
)
from reseller_api.services.auth_rate_limit import (
    AUTH_FAILURE_LIMIT_PER_MINUTE,
    _hash_ip,
    _minute_bucket,
    check_ip_auth_failure_rate,
    record_ip_auth_failure,
    seconds_until_auth_reset,
)
from reseller_api.services.security_controls import (
    GLOBAL_API_SETTING,
    invalidate_kill_switch_cache,
    is_global_api_enabled,
)


# ---------------------------------------------------------------------------
# Helper: create a fully enabled reseller + API key
# ---------------------------------------------------------------------------

_counter = 0
_lock = threading.Lock()


def _uid():
    global _counter
    with _lock:
        _counter += 1
        return 800_000 + _counter


def _make_reseller(env="test", rate_per_min=60, daily=10_000, monthly=250_000,
                   orders_min=10, daily_orders=500, monthly_orders=10_000):
    """Create an active reseller + API key; return (context, key_id, secret)."""
    uid = _uid()
    db.cur.execute(
        "INSERT INTO users(user_id, first_name, username, blocked) VALUES(?,?,?,0)",
        (uid, "Rate Limit Test", f"rl_{uid}"),
    )
    db.conn.commit()
    reseller = rdb.create_reseller(f"RL Reseller {uid}", uid, f"rl_{uid}")
    rdb.set_reseller_status(reseller["id"], "active")
    rdb.set_reseller_limits(reseller["id"],
        rate_limit_per_min=rate_per_min,
        daily_request_limit=daily,
        monthly_request_limit=monthly,
        orders_per_min=orders_min,
        daily_order_limit=daily_orders,
        monthly_order_limit=monthly_orders,
    )
    key_id, secret = rdb.create_api_key(reseller["id"], env)
    principal = authenticate_credentials(key_id, secret, env)
    ctx = ApiRequestContext(
        request_id="test",
        environment=principal.environment,
        reseller_id=principal.reseller_id,
        reseller_public_id=principal.reseller_public_id,
        api_key_id=principal.api_key_id,
        api_key_public_id=principal.api_key_public_id,
    )
    return ctx, key_id, secret, reseller["id"]


def _minute_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _past_minute():
    """Return a minute bucket string from 2 minutes ago (different window)."""
    past = datetime.now(timezone.utc) - timedelta(minutes=2)
    return past.strftime("%Y-%m-%d %H:%M")


# ===========================================================================
# 1. IP Auth-Failure Rate Limiter
# ===========================================================================


class TestIpAuthFailureLimit(unittest.TestCase):
    """Test the brute-force IP auth failure counter."""

    def _clean_ip(self, ip):
        """Remove any existing counters for this IP+bucket so tests are isolated."""
        bucket, _ = _minute_bucket()
        ip_hash = _hash_ip(ip)
        db.cur.execute(
            "DELETE FROM reseller_ip_auth_counters WHERE ip_hash=? AND minute_bucket=?",
            (ip_hash, bucket),
        )
        db.conn.commit()

    def test_fresh_ip_is_allowed(self):
        ip = f"10.0.1.{_uid() % 255}"
        self._clean_ip(ip)
        allowed, count = check_ip_auth_failure_rate(ip)
        self.assertTrue(allowed)
        self.assertEqual(count, 0)

    def test_below_limit_is_allowed(self):
        ip = f"10.0.2.{_uid() % 255}"
        self._clean_ip(ip)
        for _ in range(AUTH_FAILURE_LIMIT_PER_MINUTE - 1):
            record_ip_auth_failure(ip)
        allowed, count = check_ip_auth_failure_rate(ip)
        self.assertTrue(allowed)
        self.assertEqual(count, AUTH_FAILURE_LIMIT_PER_MINUTE - 1)

    def test_exactly_at_limit_is_blocked(self):
        ip = f"10.0.3.{_uid() % 255}"
        self._clean_ip(ip)
        for _ in range(AUTH_FAILURE_LIMIT_PER_MINUTE):
            record_ip_auth_failure(ip)
        allowed, count = check_ip_auth_failure_rate(ip)
        self.assertFalse(allowed)
        self.assertEqual(count, AUTH_FAILURE_LIMIT_PER_MINUTE)

    def test_above_limit_is_blocked(self):
        ip = f"10.0.4.{_uid() % 255}"
        self._clean_ip(ip)
        for _ in range(AUTH_FAILURE_LIMIT_PER_MINUTE + 5):
            record_ip_auth_failure(ip)
        allowed, count = check_ip_auth_failure_rate(ip)
        self.assertFalse(allowed)
        self.assertGreaterEqual(count, AUTH_FAILURE_LIMIT_PER_MINUTE)

    def test_different_ips_are_independent(self):
        ip_a = f"192.168.1.{_uid() % 200}"
        ip_b = f"192.168.2.{_uid() % 200}"
        self._clean_ip(ip_a)
        self._clean_ip(ip_b)
        for _ in range(AUTH_FAILURE_LIMIT_PER_MINUTE):
            record_ip_auth_failure(ip_a)
        allowed_a, _ = check_ip_auth_failure_rate(ip_a)
        allowed_b, _ = check_ip_auth_failure_rate(ip_b)
        self.assertFalse(allowed_a)  # A is blocked
        self.assertTrue(allowed_b)   # B is untouched

    def test_counter_increments_atomically(self):
        ip = f"10.1.0.{_uid() % 255}"
        self._clean_ip(ip)
        results = []
        def _fail():
            results.append(record_ip_auth_failure(ip))
        threads = [threading.Thread(target=_fail) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # All 5 increments must be recorded (no lost writes)
        _, final_count = check_ip_auth_failure_rate(ip)
        self.assertEqual(final_count, 5)

    def test_new_minute_bucket_resets_counter(self):
        """Failure counter in a past minute does not affect the current minute."""
        ip = f"10.2.0.{_uid() % 255}"
        ip_hash = _hash_ip(ip)
        past_bucket = _past_minute()
        # Manually stuff the past bucket over the limit
        db.cur.execute(
            """
            INSERT INTO reseller_ip_auth_counters(ip_hash, minute_bucket, failure_count)
            VALUES(?, ?, ?)
            ON CONFLICT(ip_hash, minute_bucket) DO UPDATE SET
                failure_count=excluded.failure_count
            """,
            (ip_hash, past_bucket, AUTH_FAILURE_LIMIT_PER_MINUTE + 10),
        )
        db.conn.commit()
        # Current minute has 0 failures — should be allowed
        current_bucket, _ = _minute_bucket()
        db.cur.execute(
            "DELETE FROM reseller_ip_auth_counters WHERE ip_hash=? AND minute_bucket=?",
            (ip_hash, current_bucket),
        )
        db.conn.commit()
        allowed, count = check_ip_auth_failure_rate(ip)
        self.assertTrue(allowed)
        self.assertEqual(count, 0)

    def test_seconds_until_reset_is_positive(self):
        reset = seconds_until_auth_reset()
        self.assertGreater(reset, 0)
        self.assertLessEqual(reset, 60)

    def test_ip_raw_not_stored(self):
        """Plain IPs must never appear in reseller_ip_auth_counters."""
        ip = "203.0.113.42"
        self._clean_ip(ip)
        record_ip_auth_failure(ip)
        db.cur.execute(
            "SELECT ip_hash FROM reseller_ip_auth_counters WHERE ip_hash=?",
            (ip,),  # exact plain IP should not match
        )
        self.assertIsNone(db.cur.fetchone(), "Raw IP stored — must not be")

    def test_ipv6_is_hashed_and_counted(self):
        ipv6 = "2001:db8::1"
        self._clean_ip(ipv6)
        record_ip_auth_failure(ipv6)
        allowed, count = check_ip_auth_failure_rate(ipv6)
        self.assertTrue(allowed)  # 1 failure, well below limit
        self.assertEqual(count, 1)


# ===========================================================================
# 2. Global Per-Reseller Request Rate Limiter
# ===========================================================================


class TestGlobalRequestRateLimit(unittest.TestCase):
    """Test evaluate_request_rate() — the existing per-reseller DB quota."""

    def setUp(self):
        self.ctx, self.key_id, self.secret, self.rid = _make_reseller(
            rate_per_min=5,  # low limit for fast testing
            daily=10_000,
            monthly=250_000,
        )

    def _eval(self, ctx=None):
        return evaluate_request_rate(ctx or self.ctx, "test")

    def test_first_request_is_allowed(self):
        d = self._eval()
        self.assertTrue(d.allowed)
        self.assertEqual(d.reason, "allowed")

    def test_requests_below_limit_are_allowed(self):
        for i in range(4):
            d = self._eval()
            self.assertTrue(d.allowed, f"request {i+1} should be allowed")

    def test_exactly_at_limit_is_allowed(self):
        # The 5th request (limit=5) should still be allowed
        for _ in range(4):
            self._eval()
        d = self._eval()  # 5th
        self.assertTrue(d.allowed)

    def test_above_limit_is_denied_429(self):
        for _ in range(5):
            self._eval()
        d = self._eval()  # 6th — over limit
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "rate_limit_exceeded")

    def test_headers_on_allowed_response(self):
        d = self._eval()
        headers = d.headers()
        self.assertIn("RateLimit-Limit", headers)
        self.assertIn("RateLimit-Remaining", headers)
        self.assertIn("RateLimit-Reset", headers)
        self.assertNotIn("Retry-After", headers)  # not on allowed

    def test_headers_on_denied_response(self):
        for _ in range(5):
            self._eval()
        d = self._eval()  # denied
        headers = d.headers(include_retry=True)
        self.assertIn("Retry-After", headers)
        retry = int(headers["Retry-After"])
        self.assertGreater(retry, 0)
        self.assertLessEqual(retry, 60)

    def test_remaining_decrements_correctly(self):
        d1 = self._eval()
        d2 = self._eval()
        # remaining should decrease (or stay due to minute boundary edge case)
        self.assertGreaterEqual(d1.remaining, d2.remaining)

    def test_limit_field_matches_configured_value(self):
        d = self._eval()
        self.assertEqual(d.limit, 5)

    def test_new_minute_bucket_resets_quota(self):
        """Directly manipulate the quota counter to simulate a new minute."""
        # Exhaust the limit
        for _ in range(5):
            self._eval()
        d = self._eval()
        self.assertFalse(d.allowed)  # confirm exhausted

        # Simulate rolling to a new minute by clearing the counter
        db.cur.execute(
            "UPDATE reseller_api_quota_counters SET "
            "request_minute_bucket='1970-01-01 00:00', request_minute_count=0 "
            "WHERE reseller_id=?",
            (self.rid,),
        )
        db.conn.commit()

        d2 = self._eval()
        self.assertTrue(d2.allowed)

    def test_identity_not_authorized_for_disabled_api(self):
        """Reseller with api_enabled=0 gets identity_not_authorized."""
        ctx, _, _, rid = _make_reseller(rate_per_min=60)
        db.cur.execute(
            "UPDATE resellers SET api_enabled=0 WHERE id=?", (rid,)
        )
        db.conn.commit()
        d = evaluate_request_rate(ctx, "test")
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "identity_not_authorized")

    def test_rate_limiter_unavailable_on_db_error(self):
        """DB exception → rate_limiter_unavailable (fails closed as 503)."""
        ctx, _, _, _ = _make_reseller(rate_per_min=60)
        import database.reseller_db as rdb2
        orig = rdb2.consume_api_request_quota
        rdb2.consume_api_request_quota = staticmethod(
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db gone"))
        )
        try:
            d = evaluate_request_rate(ctx, "test")
            self.assertFalse(d.allowed)
            self.assertEqual(d.reason, "rate_limiter_unavailable")
        finally:
            rdb2.consume_api_request_quota = orig


# ===========================================================================
# 3. Reseller Quota Isolation
# ===========================================================================


class TestResellerQuotaIsolation(unittest.TestCase):
    """One reseller's usage must never consume another reseller's quota."""

    def setUp(self):
        self.ctx_a, _, _, self.rid_a = _make_reseller(rate_per_min=3)
        self.ctx_b, _, _, self.rid_b = _make_reseller(rate_per_min=3)

    def test_resellers_have_independent_quotas(self):
        # Exhaust A
        for _ in range(3):
            evaluate_request_rate(self.ctx_a, "test")
        d_a = evaluate_request_rate(self.ctx_a, "test")
        d_b = evaluate_request_rate(self.ctx_b, "test")
        self.assertFalse(d_a.allowed, "Reseller A should be rate-limited")
        self.assertTrue(d_b.allowed, "Reseller B should be unaffected")

    def test_reseller_b_cannot_consume_reseller_a_quota(self):
        # Make 100 requests as B (well above limit)
        for _ in range(100):
            evaluate_request_rate(self.ctx_b, "test")
        # A's first request should still succeed
        d = evaluate_request_rate(self.ctx_a, "test")
        self.assertTrue(d.allowed)

    def test_reseller_ids_are_different(self):
        self.assertNotEqual(self.rid_a, self.rid_b)


# ===========================================================================
# 4. Multiple API Keys Share One Reseller Quota
# ===========================================================================


class TestMultipleApiKeysShareQuota(unittest.TestCase):
    """Two keys for the same reseller share the same aggregate limit."""

    def setUp(self):
        self.ctx1, self.key1, self.secret1, self.rid = _make_reseller(rate_per_min=4)
        # Create a second key for the same reseller
        self.key2, self.secret2 = rdb.create_api_key(self.rid, "test")
        p2 = authenticate_credentials(self.key2, self.secret2, "test")
        self.ctx2 = ApiRequestContext(
            request_id="test2",
            environment=p2.environment,
            reseller_id=p2.reseller_id,
            reseller_public_id=p2.reseller_public_id,
            api_key_id=p2.api_key_id,
            api_key_public_id=p2.api_key_public_id,
        )

    def test_two_keys_share_reseller_quota(self):
        # Use 2 requests via key1, 2 via key2 = 4 total (at limit)
        evaluate_request_rate(self.ctx1, "test")
        evaluate_request_rate(self.ctx1, "test")
        evaluate_request_rate(self.ctx2, "test")
        evaluate_request_rate(self.ctx2, "test")
        # 5th request (any key) should be denied
        d = evaluate_request_rate(self.ctx1, "test")
        self.assertFalse(d.allowed, "Shared reseller quota should be exhausted")

    def test_key_hopping_does_not_bypass_limit(self):
        """Alternating between keys cannot exceed the reseller limit."""
        for i in range(4):
            ctx = self.ctx1 if i % 2 == 0 else self.ctx2
            evaluate_request_rate(ctx, "test")
        d = evaluate_request_rate(self.ctx1, "test")
        self.assertFalse(d.allowed)


# ===========================================================================
# 5. Revoked / Invalid Key Scenarios
# ===========================================================================


class TestAuthKeyScenarios(unittest.TestCase):
    """Auth failures from revoked/invalid keys increment the IP failure counter."""

    def setUp(self):
        self.ctx, self.key_id, self.secret, self.rid = _make_reseller()

    def test_valid_key_authenticates(self):
        principal = authenticate_credentials(self.key_id, self.secret, "test")
        self.assertIsNotNone(principal)

    def test_wrong_secret_fails_auth(self):
        principal = authenticate_credentials(self.key_id, "wrongsecret" * 4, "test")
        self.assertIsNone(principal)

    def test_revoked_key_fails_auth(self):
        rdb.revoke_api_key(self.key_id)
        principal = authenticate_credentials(self.key_id, self.secret, "test")
        self.assertIsNone(principal)

    def test_nonexistent_key_fails_auth(self):
        principal = authenticate_credentials("rk_test_ZZZZZZZZ", "a" * 32, "test")
        self.assertIsNone(principal)

    def test_wrong_environment_fails_auth(self):
        principal = authenticate_credentials(self.key_id, self.secret, "production")
        self.assertIsNone(principal)

    def test_blocked_reseller_fails_auth(self):
        db.cur.execute(
            "UPDATE resellers SET status=? WHERE id=?",
            ("suspended", self.rid),
        )
        db.conn.commit()
        principal = authenticate_credentials(self.key_id, self.secret, "test")
        self.assertIsNone(principal)

    def test_auth_failure_increments_ip_counter(self):
        ip = f"10.5.{_uid() % 255}.1"
        bucket, _ = _minute_bucket()
        ip_hash = _hash_ip(ip)
        db.cur.execute(
            "DELETE FROM reseller_ip_auth_counters WHERE ip_hash=? AND minute_bucket=?",
            (ip_hash, bucket),
        )
        db.conn.commit()
        # Record failures via the service (simulating middleware)
        record_ip_auth_failure(ip)
        record_ip_auth_failure(ip)
        _, count = check_ip_auth_failure_rate(ip)
        self.assertEqual(count, 2)

    def test_success_does_not_increment_ip_counter(self):
        """Successful auth must NOT call record_ip_auth_failure."""
        ip = f"10.6.{_uid() % 255}.1"
        bucket, _ = _minute_bucket()
        ip_hash = _hash_ip(ip)
        db.cur.execute(
            "DELETE FROM reseller_ip_auth_counters WHERE ip_hash=? AND minute_bucket=?",
            (ip_hash, bucket),
        )
        db.conn.commit()
        # Successful auth — middleware does NOT call record_ip_auth_failure on success
        principal = authenticate_credentials(self.key_id, self.secret, "test")
        self.assertIsNotNone(principal)
        _, count = check_ip_auth_failure_rate(ip)
        self.assertEqual(count, 0)


# ===========================================================================
# 6. Kill-Switch TTL Cache
# ===========================================================================


class TestKillSwitchCache(unittest.TestCase):
    """Verify the process-local 30-second TTL cache on the kill-switch."""

    def setUp(self):
        invalidate_kill_switch_cache()

    def tearDown(self):
        # Restore enabled state and clear cache
        db.set_setting(GLOBAL_API_SETTING, "1")
        invalidate_kill_switch_cache()

    def test_enabled_when_setting_is_1(self):
        db.set_setting(GLOBAL_API_SETTING, "1")
        invalidate_kill_switch_cache()
        self.assertTrue(is_global_api_enabled())

    def test_disabled_when_setting_is_0(self):
        db.set_setting(GLOBAL_API_SETTING, "0")
        invalidate_kill_switch_cache()
        self.assertFalse(is_global_api_enabled())

    def test_cached_value_served_within_ttl(self):
        db.set_setting(GLOBAL_API_SETTING, "1")
        invalidate_kill_switch_cache()
        v1 = is_global_api_enabled()  # primes cache
        # Change DB value WITHOUT invalidating cache
        db.set_setting(GLOBAL_API_SETTING, "0")
        v2 = is_global_api_enabled()  # should serve cached True
        self.assertTrue(v1)
        self.assertTrue(v2, "Cached value should still be True within TTL")

    def test_invalidate_forces_db_reread(self):
        db.set_setting(GLOBAL_API_SETTING, "1")
        invalidate_kill_switch_cache()
        is_global_api_enabled()  # prime cache
        db.set_setting(GLOBAL_API_SETTING, "0")
        invalidate_kill_switch_cache()  # force re-read
        self.assertFalse(is_global_api_enabled())

    def test_fail_closed_on_db_error(self):
        """DB error → kill-switch returns False (API unavailable — fail closed)."""
        import database.db as botdb2
        orig = botdb2.get_setting
        botdb2.get_setting = staticmethod(
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("DB gone"))
        )
        invalidate_kill_switch_cache()
        try:
            result = is_global_api_enabled()
            self.assertFalse(result, "Should fail closed on DB error")
        finally:
            botdb2.get_setting = orig
            invalidate_kill_switch_cache()

    def test_multiple_values(self):
        for value, expected in [("1", True), ("true", True), ("yes", True),
                                 ("on", True), ("0", False), ("false", False),
                                 ("off", False), ("", False)]:
            db.set_setting(GLOBAL_API_SETTING, value)
            invalidate_kill_switch_cache()
            self.assertEqual(is_global_api_enabled(), expected,
                             f"Setting={value!r} expected={expected}")


# ===========================================================================
# 7. Distributed Safety (Thread Simulation)
# ===========================================================================


class TestDistributedSafety(unittest.TestCase):
    """Simulate multiple concurrent API instances hitting the same rate limiter."""

    def test_concurrent_requests_same_reseller_atomic(self):
        """Concurrent quota consumption must not allow more than limit requests."""
        ctx, _, _, _ = _make_reseller(rate_per_min=10)
        results = []
        errors = []

        def _make_request():
            try:
                d = evaluate_request_rate(ctx, "test")
                results.append(d.allowed)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=_make_request) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")
        allowed_count = sum(1 for r in results if r)
        # With limit=10 and 20 concurrent requests, at most 10 should be allowed
        # (the atomic UPSERT ensures no over-counting)
        self.assertLessEqual(allowed_count, 10,
                             f"Allowed {allowed_count} requests; limit is 10")
        # At least some should have been allowed (not all blocked)
        self.assertGreater(allowed_count, 0)

    def test_concurrent_ip_failure_counters_atomic(self):
        """Concurrent failure increments must not lose any writes."""
        ip = f"172.16.{_uid() % 255}.1"
        bucket, _ = _minute_bucket()
        ip_hash = _hash_ip(ip)
        db.cur.execute(
            "DELETE FROM reseller_ip_auth_counters WHERE ip_hash=? AND minute_bucket=?",
            (ip_hash, bucket),
        )
        db.conn.commit()

        n = 15
        errors = []

        def _fail():
            try:
                record_ip_auth_failure(ip)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=_fail) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        _, count = check_ip_auth_failure_rate(ip)
        self.assertEqual(count, n, f"Expected {n} failures, got {count}")

    def test_multiple_resellers_concurrent_isolation(self):
        """Concurrent requests from N resellers must not bleed into each other."""
        resellers = [_make_reseller(rate_per_min=5) for _ in range(3)]
        allowed_per = {r[3]: [] for r in resellers}  # rid -> [allowed bools]
        errors = []

        def _run(ctx, rid):
            try:
                d = evaluate_request_rate(ctx, "test")
                allowed_per[rid].append(d.allowed)
            except Exception as e:
                errors.append(str(e))

        threads = []
        for ctx, _, _, rid in resellers:
            for _ in range(8):  # 8 requests each; limit=5
                threads.append(threading.Thread(target=_run, args=(ctx, rid)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        for ctx, _, _, rid in resellers:
            allowed = sum(1 for v in allowed_per[rid] if v)
            self.assertLessEqual(allowed, 5,
                                 f"Reseller {rid} allowed {allowed} (limit 5)")


# ===========================================================================
# 8. Rate Limit Response Format
# ===========================================================================


class TestRateLimitResponseFormat(unittest.TestCase):
    """Verify 429 response shape matches the project standard."""

    def test_decision_dataclass_fields(self):
        ctx, _, _, _ = _make_reseller(rate_per_min=1)
        evaluate_request_rate(ctx, "test")  # consume
        d = evaluate_request_rate(ctx, "test")  # denied
        self.assertIsInstance(d, RequestRateDecision)
        self.assertFalse(d.allowed)
        self.assertIsInstance(d.limit, int)
        self.assertIsInstance(d.remaining, int)
        self.assertIsInstance(d.reset_after, int)
        self.assertIsInstance(d.scope, str)
        self.assertEqual(d.remaining, 0)

    def test_no_internal_fields_in_headers(self):
        ctx, _, _, _ = _make_reseller(rate_per_min=1)
        evaluate_request_rate(ctx, "test")
        d = evaluate_request_rate(ctx, "test")
        headers = d.headers(include_retry=True)
        # Internal fields must not appear in headers
        for key in headers:
            self.assertNotIn("reseller", key.lower())
            self.assertNotIn("key", key.lower())
            self.assertNotIn("secret", key.lower())


if __name__ == "__main__":
    unittest.main()
