
"""Phase 10 Prompt 6 - Auth abuse & API key protection tests."""
from __future__ import annotations
import os, sys, tempfile, threading, unittest
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
_fd, _db_path = tempfile.mkstemp(prefix="oggy_auth_abuse_", suffix=".db")
os.close(_fd)
import config; config.DB_PATH = _db_path
import database.db as db; db.DB_PATH = _db_path; db._tls = threading.local(); db.init_db()
import database.reseller_db as rdb
from reseller_api.services.authentication import authenticate_credentials
from reseller_api.services.auth_rate_limit import (
    AUTH_FAILURE_LIMIT_PER_MINUTE,
    check_ip_auth_failure_rate, record_ip_auth_failure,
)
from database.reseller_db import get_key_auth_failure_count, increment_key_auth_failure

_ctr = 0; _lock = threading.Lock()
def _uid():
    global _ctr
    with _lock: _ctr += 1; return _ctr

def _now(): return datetime.now(timezone.utc)

def _make_active_reseller(env="test"):
    uid = _uid()
    rs = rdb.create_reseller(f"AuthAbuseTester-{uid}", username=f"authabuse{uid}")
    rdb.set_reseller_status(rs["id"], "active")
    key_id, secret = rdb.create_api_key(rs["id"], environment=env)
    return rs, key_id, secret

def _bucket_str(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

# check_ip_auth_failure_rate returns tuple (allowed: bool, count: int)
# allowed=True means the IP is NOT yet blocked
def _ip_allowed(ip, moment=None): return check_ip_auth_failure_rate(ip, moment)[0]


class TestValidKeyAuthentication(unittest.TestCase):
    def test_valid_key_succeeds(self):
        rs, key_id, secret = _make_active_reseller()
        self.assertIsNotNone(authenticate_credentials(key_id, secret, "test"))
    def test_environment_mismatch_rejected(self):
        rs, key_id, secret = _make_active_reseller(env="test")
        self.assertIsNone(authenticate_credentials(key_id, secret, "production"))


class TestInvalidKeyRejection(unittest.TestCase):
    def test_completely_invalid_returns_none(self):
        self.assertIsNone(authenticate_credentials("invalid", "cred", "test"))
    def test_unknown_key_id_returns_none(self):
        self.assertIsNone(authenticate_credentials("UNKNOWN_KEY_ID", "randomsecret", "test"))
    def test_wrong_secret_returns_none(self):
        rs, key_id, secret = _make_active_reseller()
        self.assertIsNone(authenticate_credentials(key_id, "wrongsecret", "test"))
    def test_none_key_id_returns_none(self):
        self.assertIsNone(authenticate_credentials(None, None, "test"))


class TestRevokedKeyRejection(unittest.TestCase):
    def test_revoked_key_rejected(self):
        rs, key_id, secret = _make_active_reseller()
        rdb.revoke_api_key(key_id)
        self.assertIsNone(authenticate_credentials(key_id, secret, "test"))
    def test_disabled_key_rejected(self):
        rs, key_id, secret = _make_active_reseller()
        rdb.set_api_key_enabled_for_reseller(rs["id"], key_id, False)
        self.assertIsNone(authenticate_credentials(key_id, secret, "test"))


class TestSuspendedReseller(unittest.TestCase):
    def test_suspended_rejected(self):
        rs, key_id, secret = _make_active_reseller()
        rdb.set_reseller_status(rs["id"], "suspended")
        self.assertIsNone(authenticate_credentials(key_id, secret, "test"))
    def test_closed_rejected(self):
        rs, key_id, secret = _make_active_reseller()
        rdb.set_reseller_status(rs["id"], "closed")
        self.assertIsNone(authenticate_credentials(key_id, secret, "test"))
    def test_pending_rejected(self):
        rs, key_id, secret = _make_active_reseller()
        rdb.set_reseller_status(rs["id"], "pending")
        self.assertIsNone(authenticate_credentials(key_id, secret, "test"))


class TestErrorUniformity(unittest.TestCase):
    def test_all_failures_return_none(self):
        rs, key_id, secret = _make_active_reseller()
        rdb.revoke_api_key(key_id)
        self.assertIsNone(authenticate_credentials(key_id, secret, "test"))
        self.assertIsNone(authenticate_credentials("bogus", "cred", "test"))
        self.assertIsNone(authenticate_credentials(key_id, "wrongsecret", "test"))


class TestIPAuthFailureRateLimit(unittest.TestCase):
    def test_first_failure_allowed(self):
        ip = f"10.0.{_uid()}.1"; t = _now()
        record_ip_auth_failure(ip, t)
        self.assertTrue(_ip_allowed(ip, t))  # 1 failure < limit, still allowed
    def test_exceeding_limit_blocked(self):
        ip = f"10.1.{_uid()}.1"; t = _now()
        for _ in range(AUTH_FAILURE_LIMIT_PER_MINUTE):
            record_ip_auth_failure(ip, t)
        self.assertFalse(_ip_allowed(ip, t))  # at/above limit, blocked
    def test_different_ips_independent(self):
        ip_a = f"10.2.{_uid()}.1"; ip_b = f"10.2.{_uid()}.2"; t = _now()
        for _ in range(AUTH_FAILURE_LIMIT_PER_MINUTE):
            record_ip_auth_failure(ip_a, t)
        self.assertFalse(_ip_allowed(ip_a, t))
        self.assertTrue(_ip_allowed(ip_b, t))


class TestKeyPrefixFailureCounter(unittest.TestCase):
    def test_increment_returns_positive(self):
        prefix = f"TESTKEY{_uid()}"; bucket = _bucket_str()
        count = increment_key_auth_failure(prefix, bucket)
        self.assertGreater(count, 0)
    def test_repeated_increments_accumulate(self):
        prefix = f"TESTKEY{_uid()}"; bucket = _bucket_str()
        for _ in range(5): increment_key_auth_failure(prefix, bucket)
        self.assertGreaterEqual(get_key_auth_failure_count(prefix, bucket), 5)
    def test_different_prefixes_independent(self):
        prefix_a = f"KEYA{_uid()}"; prefix_b = f"KEYB{_uid()}"; bucket = _bucket_str()
        for _ in range(3): increment_key_auth_failure(prefix_a, bucket)
        self.assertEqual(get_key_auth_failure_count(prefix_b, bucket), 0)
    def test_zero_for_new_prefix(self):
        self.assertEqual(get_key_auth_failure_count(f"NEW{_uid()}", _bucket_str()), 0)


class TestLoggingSafety(unittest.TestCase):
    def test_no_raw_secret_in_logs(self):
        import logging
        rs, key_id, secret = _make_active_reseller()
        records = []
        class Cap(logging.Handler):
            def emit(self, r): records.append(self.format(r))
        cap = Cap(); root = logging.getLogger(); root.addHandler(cap)
        try:
            authenticate_credentials(key_id, secret, "test")
            authenticate_credentials(key_id, "wrongsecret", "test")
        finally: root.removeHandler(cap)
        for r in records:
            self.assertNotIn(secret, r, "Raw secret must not appear in logs")


if __name__ == "__main__":
    unittest.main()
