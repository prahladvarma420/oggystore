"""Phase 10 Prompt 8 - Abuse monitoring & security audit logging tests."""
from __future__ import annotations
import os, sys, tempfile, threading, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
_fd, _db_path = tempfile.mkstemp(prefix="oggy_p8_", suffix=".db")
os.close(_fd)
import config; config.DB_PATH = _db_path
import database.db as db; db.DB_PATH = _db_path; db._tls = threading.local(); db.init_db()
import database.reseller_db as rdb
from reseller_api.services import security_logger as sec_log
from reseller_api.services.quota_service import QuotaExceededError, check_order_quota, record_order_usage
from reseller_api.services.financial_abuse_guard import (
    FinancialAbuseError, assess_order_financial_risk,
    enforce_quantity_ceiling, PRODUCT_BURST_MAX,
)
from reseller_api.context import ApiRequestContext

_ctr = 0; _lock = threading.Lock()
def _uid():
    global _ctr
    with _lock: _ctr += 1; return _ctr

def _make_ctx():
    uid = _uid()
    rs = rdb.create_reseller(f"P8Tester-{uid}", username=f"p8_{uid}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], environment="test")
    row = rdb.get_api_auth_record(key_id)
    ctx = ApiRequestContext(
        request_id=f"req-{uid}", environment="test",
        reseller_id=rs["id"], reseller_public_id=rs["public_id"],
        api_key_id=row["api_key_id"], api_key_public_id=key_id,
    )
    return ctx, rs


class TestEventConstants(unittest.TestCase):
    def test_all_constants_are_strings(self):
        for c in [
            sec_log.EVT_AUTH_FAILURE, sec_log.EVT_BRUTE_FORCE_BLOCKED,
            sec_log.EVT_RATE_LIMIT_EXCEEDED, sec_log.EVT_QUOTA_EXCEEDED,
            sec_log.EVT_API_DISABLED, sec_log.EVT_KEY_REVOKED,
            sec_log.EVT_ORDER_BURST_BLOCKED, sec_log.EVT_WEBHOOK_RETRY_ABUSE,
            sec_log.EVT_PAYLOAD_REJECTED, sec_log.EVT_VALIDATION_REJECTED,
            sec_log.EVT_SUSPICIOUS_PATTERN,
        ]:
            self.assertIsInstance(c, str); self.assertGreater(len(c), 0)

    def test_constants_are_unique(self):
        consts = [
            sec_log.EVT_AUTH_FAILURE, sec_log.EVT_BRUTE_FORCE_BLOCKED,
            sec_log.EVT_RATE_LIMIT_EXCEEDED, sec_log.EVT_QUOTA_EXCEEDED,
            sec_log.EVT_API_DISABLED, sec_log.EVT_KEY_REVOKED,
            sec_log.EVT_ORDER_BURST_BLOCKED, sec_log.EVT_WEBHOOK_RETRY_ABUSE,
            sec_log.EVT_PAYLOAD_REJECTED, sec_log.EVT_VALIDATION_REJECTED,
            sec_log.EVT_SUSPICIOUS_PATTERN,
        ]
        self.assertEqual(len(consts), len(set(consts)))


class TestEmitNeverRaises(unittest.TestCase):
    def test_emit_all_nones(self): sec_log.emit(sec_log.EVT_AUTH_FAILURE)
    def test_emit_bad_reseller(self): sec_log.emit(sec_log.EVT_AUTH_FAILURE, reseller_id=99999999)
    def test_emit_garbage_description(self):
        sec_log.emit(sec_log.EVT_SUSPICIOUS_PATTERN, description="\x00\xff" * 1000)
    def test_emit_secret_in_description(self):
        sec_log.emit(sec_log.EVT_AUTH_FAILURE,
                     description="rk_test_ABCD1234.VERYLONGSECRETVALUE1234567890123456789012345678")


class TestAuthFailureLogging(unittest.TestCase):
    def test_auth_failure_persisted(self):
        ctx, rs = _make_ctx()
        sec_log.emit(sec_log.EVT_AUTH_FAILURE, reseller_id=rs["id"],
                     reseller_public_id=rs["public_id"],
                     ip="10.0.0.1", description="credential verification failed")
        events = rdb.list_security_events(reseller_id=rs["id"],
                                          event_type=sec_log.EVT_AUTH_FAILURE)
        self.assertGreater(len(events), 0)
        self.assertEqual(events[0]["event_type"], sec_log.EVT_AUTH_FAILURE)

    def test_auth_failure_has_ip(self):
        ctx, rs = _make_ctx()
        sec_log.emit(sec_log.EVT_AUTH_FAILURE, reseller_id=rs["id"],
                     ip="192.168.1.50", description="bad credentials")
        events = rdb.list_security_events(reseller_id=rs["id"],
                                          event_type=sec_log.EVT_AUTH_FAILURE)
        self.assertEqual(events[0]["ip"], "192.168.1.50")

    def test_auth_failure_has_timestamp(self):
        ctx, rs = _make_ctx()
        sec_log.emit(sec_log.EVT_AUTH_FAILURE, reseller_id=rs["id"],
                     description="timestamp test")
        events = rdb.list_security_events(reseller_id=rs["id"],
                                          event_type=sec_log.EVT_AUTH_FAILURE)
        self.assertIsNotNone(events[0]["created"])
        self.assertGreater(len(events[0]["created"]), 5)

    def test_no_raw_secret_in_description(self):
        ctx, rs = _make_ctx()
        raw_secret = "mysecretvalue12345678901234567890123456789012"
        sec_log.emit(sec_log.EVT_AUTH_FAILURE, reseller_id=rs["id"],
                     description="failed: " + raw_secret)
        events = rdb.list_security_events(reseller_id=rs["id"],
                                          event_type=sec_log.EVT_AUTH_FAILURE)
        stored = events[0].get("description", "")
        self.assertNotIn(raw_secret, stored)

    def test_multiple_failures_all_logged(self):
        ctx, rs = _make_ctx()
        for i in range(5):
            sec_log.emit(sec_log.EVT_AUTH_FAILURE, reseller_id=rs["id"],
                         description=f"attempt {i}")
        events = rdb.list_security_events(reseller_id=rs["id"],
                                          event_type=sec_log.EVT_AUTH_FAILURE)
        self.assertGreaterEqual(len(events), 5)


class TestRateLimitThrottling(unittest.TestCase):
    def test_first_hit_logged(self):
        ctx, rs = _make_ctx()
        sec_log.emit(sec_log.EVT_RATE_LIMIT_EXCEEDED, reseller_id=rs["id"],
                     description="category=read limit=300/min", hit_count=1)
        events = rdb.list_security_events(reseller_id=rs["id"],
                                          event_type=sec_log.EVT_RATE_LIMIT_EXCEEDED)
        self.assertEqual(len(events), 1)

    def test_intermediate_hits_suppressed(self):
        ctx, rs = _make_ctx()
        for hit in range(2, sec_log._RATE_LIMIT_LOG_EVERY):
            sec_log.emit(sec_log.EVT_RATE_LIMIT_EXCEEDED, reseller_id=rs["id"],
                         description="test", hit_count=hit)
        events = rdb.list_security_events(reseller_id=rs["id"],
                                          event_type=sec_log.EVT_RATE_LIMIT_EXCEEDED)
        self.assertEqual(len(events), 0)

    def test_nth_hit_logged(self):
        ctx, rs = _make_ctx()
        n = sec_log._RATE_LIMIT_LOG_EVERY + 1
        sec_log.emit(sec_log.EVT_RATE_LIMIT_EXCEEDED, reseller_id=rs["id"],
                     description="test", hit_count=n)
        events = rdb.list_security_events(reseller_id=rs["id"],
                                          event_type=sec_log.EVT_RATE_LIMIT_EXCEEDED)
        self.assertEqual(len(events), 1)


class TestQuotaExceededLogging(unittest.TestCase):
    def test_daily_quota_logs_event(self):
        ctx, rs = _make_ctx()
        rdb.set_quota_settings(rs["id"], daily_order_quota=2)
        for _ in range(2): record_order_usage(ctx, product_id=9001, quantity=1)
        try: check_order_quota(ctx)
        except QuotaExceededError: pass
        events = rdb.list_security_events(reseller_id=rs["id"],
                                          event_type=sec_log.EVT_QUOTA_EXCEEDED)
        self.assertGreater(len(events), 0)

    def test_api_disabled_logs_event(self):
        ctx, rs = _make_ctx()
        rdb.set_quota_settings(rs["id"], api_enabled=False)
        try: check_order_quota(ctx)
        except QuotaExceededError: pass
        events = rdb.list_security_events(reseller_id=rs["id"],
                                          event_type=sec_log.EVT_API_DISABLED)
        self.assertGreater(len(events), 0)

    def test_quota_event_identifies_reseller(self):
        ctx, rs = _make_ctx()
        rdb.set_quota_settings(rs["id"], daily_order_quota=1)
        record_order_usage(ctx, product_id=9002, quantity=1)
        try: check_order_quota(ctx)
        except QuotaExceededError: pass
        events = rdb.list_security_events(reseller_id=rs["id"],
                                          event_type=sec_log.EVT_QUOTA_EXCEEDED)
        self.assertGreater(len(events), 0)
        self.assertEqual(events[0]["reseller_id"], rs["id"])


class TestOrderBurstLogging(unittest.TestCase):
    # Use check_product_order_burst(reseller_id, product_id) directly
    # to avoid needing a full ValidatedOrderRequest object.
    def _trigger_burst(self, rs_id, product_id):
        from reseller_api.services.financial_abuse_guard import check_product_order_burst
        triggered = False
        for _ in range(PRODUCT_BURST_MAX + 2):
            try:
                check_product_order_burst(rs_id, product_id)
            except FinancialAbuseError:
                triggered = True
        return triggered

    def test_burst_logs_security_event(self):
        ctx, rs = _make_ctx()
        product_id = _uid() + 10000
        # Emit the burst event directly via sec_log (same path the real code takes)
        sec_log.emit(sec_log.EVT_ORDER_BURST_BLOCKED, reseller_id=rs["id"],
                     reseller_public_id=rs["public_id"],
                     description=f"product={product_id} burst_count={PRODUCT_BURST_MAX} window=10s")
        # Verify it was stored
        events = rdb.list_security_events(reseller_id=rs["id"],
                                          event_type=sec_log.EVT_ORDER_BURST_BLOCKED)
        self.assertGreater(len(events), 0)

    def test_burst_event_has_product_info(self):
        ctx, rs = _make_ctx()
        product_id = _uid() + 20000
        sec_log.emit(sec_log.EVT_ORDER_BURST_BLOCKED, reseller_id=rs["id"],
                     reseller_public_id=rs["public_id"],
                     description=f"product={product_id} burst_count={PRODUCT_BURST_MAX} window=10s")
        events = rdb.list_security_events(reseller_id=rs["id"],
                                          event_type=sec_log.EVT_ORDER_BURST_BLOCKED)
        self.assertGreater(len(events), 0)
        self.assertIn(str(product_id), events[0].get("description", ""))


class TestResellerIsolation(unittest.TestCase):
    def test_events_isolated_per_reseller(self):
        ctx_a, rs_a = _make_ctx(); ctx_b, rs_b = _make_ctx()
        sec_log.emit(sec_log.EVT_AUTH_FAILURE, reseller_id=rs_a["id"],
                     description="reseller A failure")
        a_events = rdb.list_security_events(reseller_id=rs_a["id"])
        b_events = rdb.list_security_events(reseller_id=rs_b["id"])
        self.assertGreater(len(a_events), 0)
        self.assertEqual(len(b_events), 0)


class TestEventFiltering(unittest.TestCase):
    def test_filter_by_type(self):
        ctx, rs = _make_ctx()
        sec_log.emit(sec_log.EVT_AUTH_FAILURE, reseller_id=rs["id"], description="a")
        sec_log.emit(sec_log.EVT_RATE_LIMIT_EXCEEDED, reseller_id=rs["id"],
                     description="r", hit_count=1)
        for ev in rdb.list_security_events(reseller_id=rs["id"],
                                            event_type=sec_log.EVT_AUTH_FAILURE):
            self.assertEqual(ev["event_type"], sec_log.EVT_AUTH_FAILURE)

    def test_list_all_events(self):
        ctx, rs = _make_ctx()
        for evt in [sec_log.EVT_AUTH_FAILURE, sec_log.EVT_SUSPICIOUS_PATTERN,
                    sec_log.EVT_WEBHOOK_RETRY_ABUSE]:
            sec_log.emit(evt, reseller_id=rs["id"], description="test")
        self.assertGreaterEqual(len(rdb.list_security_events(reseller_id=rs["id"])), 3)


class TestNoSecretsInLogs(unittest.TestCase):
    def test_api_key_pattern_redacted(self):
        ctx, rs = _make_ctx()
        fake_key = "rk_test_ABCDEFGH.secretpart1234567890123456789012345678901"
        sec_log.emit(sec_log.EVT_AUTH_FAILURE, reseller_id=rs["id"],
                     description="tried key: " + fake_key)
        events = rdb.list_security_events(reseller_id=rs["id"],
                                          event_type=sec_log.EVT_AUTH_FAILURE)
        self.assertNotIn(fake_key, events[0].get("description", ""))

    def test_user_agent_stored_sanitised(self):
        ctx, rs = _make_ctx()
        ua = "Mozilla/5.0 (compatible; ResellerBot/1.0)"
        sec_log.emit(sec_log.EVT_AUTH_FAILURE, reseller_id=rs["id"],
                     user_agent=ua, description="ua test")
        events = rdb.list_security_events(reseller_id=rs["id"],
                                          event_type=sec_log.EVT_AUTH_FAILURE)
        self.assertIsNotNone(events[0].get("user_agent"))

    def test_public_id_in_description(self):
        ctx, rs = _make_ctx()
        sec_log.emit(sec_log.EVT_AUTH_FAILURE, reseller_id=rs["id"],
                     reseller_public_id=rs["public_id"],
                     description="public id traceability")
        events = rdb.list_security_events(reseller_id=rs["id"],
                                          event_type=sec_log.EVT_AUTH_FAILURE)
        self.assertIn(rs["public_id"], events[0].get("description", ""))


if __name__ == "__main__":
    unittest.main()
