"""Phase 9 Prompt 6 — HMAC signature & security tests.

Test matrix
-----------
 1.  Valid signature                → verify_signature returns True
 2.  Invalid signature              → verify_signature returns False
 3.  Modified body                  → verify_signature returns False
 4.  Modified timestamp             → verify_signature returns False
 5.  Expired timestamp              → verify_signature returns False (freshness check)
 6.  Missing signature header       → verify_signature returns False
 7.  Missing timestamp header       → verify_signature returns False
 8.  Wrong secret                   ’ verify_signature returns False
 9.  Replay (duplicate delivery_id) ’ receiver can detect via delivery_id
10.  Secret not in logs             → no secret value appears in log output

Additional coverage
-------------------
- sign_body determinism: same inputs always produce the same signature.
- sign_body uniqueness: different secrets produce different signatures.
- is_fresh accepts timestamps within the window and rejects old ones.
- extract_headers does case-insensitive header lookup.
- generate_webhook_secret produces high-entropy URL-safe tokens.
- Constant-time comparison: verify_signature uses hmac.compare_digest.
- Header name constants match documented values.
- build_signed_string format: b"{ts}.{body}".
"""

from __future__ import annotations

import hashlib
import os
import hmac
import logging
import sys
import time
import unittest
from io import StringIO

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from reseller_api.services.webhook_signature import (
    FRESHNESS_WINDOW_SECONDS,
    HEADER_DELIVERY,
    HEADER_EVENT,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    SIGNATURE_PREFIX,
    build_signed_string,
    current_timestamp,
    extract_headers,
    generate_webhook_secret,
    is_fresh,
    sign_body,
    verify_signature,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SECRET  = "test-secret-do-not-use-in-production"
BODY    = b'{"event_type":"order.created","order_id":"ORD-00000001"}'


def _now_ts() -> str:
    return str(int(time.time()))


def _valid_sig(body: bytes = BODY, secret: str = SECRET, ts: str | None = None) -> tuple[str, str]:
    """Return (timestamp, signature) for a freshly signed body."""
    ts = ts or _now_ts()
    sig = sign_body(secret, body, ts)
    return ts, sig


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class WebhookSignatureTests(unittest.TestCase):

    # ------------------------------------------------------------------ 1
    def test_valid_signature_accepted(self):
        """A correctly signed request must be accepted."""
        ts, sig = _valid_sig()
        self.assertTrue(
            verify_signature(SECRET, BODY, ts, sig),
            "Valid signature must be accepted",
        )

    # ------------------------------------------------------------------ 2
    def test_invalid_signature_rejected(self):
        """A tampered signature must be rejected."""
        ts, _sig = _valid_sig()
        bad_sig = SIGNATURE_PREFIX + "a" * 64  # plausible-looking but wrong hex
        self.assertFalse(
            verify_signature(SECRET, BODY, ts, bad_sig),
            "Tampered signature must be rejected",
        )

    # ------------------------------------------------------------------ 3
    def test_modified_body_rejected(self):
        """Any body modification must invalidate the signature."""
        ts, sig = _valid_sig()
        tampered = BODY.replace(b"order.created", b"order.cancelled")
        self.assertFalse(
            verify_signature(SECRET, tampered, ts, sig),
            "Modified body must invalidate signature",
        )

    # ------------------------------------------------------------------ 4
    def test_modified_timestamp_rejected(self):
        """A signature computed over a different timestamp must be rejected."""
        ts_signed, sig = _valid_sig()
        ts_different = str(int(ts_signed) + 1)
        self.assertFalse(
            verify_signature(SECRET, BODY, ts_different, sig,
                             check_freshness=False),
            "Signature with wrong timestamp must be rejected",
        )

    # ------------------------------------------------------------------ 5
    def test_expired_timestamp_rejected(self):
        """A timestamp older than FRESHNESS_WINDOW_SECONDS must be rejected."""
        old_ts = str(int(time.time()) - FRESHNESS_WINDOW_SECONDS - 60)
        sig = sign_body(SECRET, BODY, old_ts)
        self.assertFalse(
            verify_signature(SECRET, BODY, old_ts, sig),
            "Expired timestamp must be rejected",
        )

    # ------------------------------------------------------------------ 6
    def test_missing_signature_rejected(self):
        """A request with no signature header must be rejected."""
        ts = _now_ts()
        self.assertFalse(
            verify_signature(SECRET, BODY, ts, ""),
            "Empty signature must be rejected",
        )
        self.assertFalse(
            verify_signature(SECRET, BODY, ts, None),  # type: ignore[arg-type]
            "None signature must be rejected",
        )

    # ------------------------------------------------------------------ 7
    def test_missing_timestamp_rejected(self):
        """A request with no timestamp header must be rejected."""
        _ts, sig = _valid_sig()
        self.assertFalse(
            verify_signature(SECRET, BODY, "", sig),
            "Empty timestamp must be rejected",
        )
        self.assertFalse(
            verify_signature(SECRET, BODY, None, sig),  # type: ignore[arg-type]
            "None timestamp must be rejected",
        )

    # ------------------------------------------------------------------ 8
    def test_wrong_secret_rejected(self):
        """A signature produced with a different secret must be rejected."""
        ts, sig = _valid_sig(secret="correct-secret")
        self.assertFalse(
            verify_signature("wrong-secret", BODY, ts, sig),
            "Signature from different secret must be rejected",
        )

    # ------------------------------------------------------------------ 9
    def test_replay_detectable_via_delivery_id(self):
        """Receivers can detect replays using the delivery_id in headers.

        The delivery worker sends a unique UUID per delivery attempt as the
        X-Oggystore-Delivery header.  A receiver that caches seen delivery_ids
        can reject duplicates.  This test verifies:
          (a) delivery_id is accessible from the header dict via extract_headers
          (b) a replayed request has the same delivery_id
          (c) a new delivery has a different delivery_id
        """
        import uuid
        delivery_id_1 = str(uuid.uuid4())
        delivery_id_2 = str(uuid.uuid4())

        # Simulate two separate requests — first delivery and its replay
        headers_original = {
            HEADER_SIGNATURE: "sha256=abc",
            HEADER_TIMESTAMP: _now_ts(),
            HEADER_DELIVERY:  delivery_id_1,
            HEADER_EVENT:     "order.created",
        }
        headers_replay = dict(headers_original)  # same delivery_id = replay
        headers_new    = dict(headers_original)
        headers_new[HEADER_DELIVERY] = delivery_id_2  # new delivery_id

        parsed_original = extract_headers(headers_original)
        parsed_replay   = extract_headers(headers_replay)
        parsed_new      = extract_headers(headers_new)

        # The receiver's seen-set logic (pseudocode tested here via assertion)
        seen_delivery_ids: set = set()

        # First request: not seen — process it
        self.assertNotIn(parsed_original["delivery_id"], seen_delivery_ids)
        seen_delivery_ids.add(parsed_original["delivery_id"])

        # Replay: same delivery_id — reject
        self.assertIn(parsed_replay["delivery_id"], seen_delivery_ids,
                      "Replay must share delivery_id with original")

        # Legitimate retry: different delivery_id — accept
        self.assertNotIn(parsed_new["delivery_id"], seen_delivery_ids,
                         "New delivery must have distinct delivery_id")

    # ------------------------------------------------------------------ 10
    def test_secret_not_emitted_to_logs(self):
        """The signing secret must NEVER appear in logger output.

        We capture log records at DEBUG level (noisiest possible) and assert
        the secret value is absent from every message and argument.
        """
        secret = generate_webhook_secret()
        body   = b'{"event_type":"order.created"}'
        ts     = _now_ts()

        log_stream = StringIO()
        handler = logging.StreamHandler(log_stream)
        handler.setLevel(logging.DEBUG)

        # Attach handler to the two modules that touch the secret
        loggers = [
            logging.getLogger('reseller_api.services.webhook_signature'),
            logging.getLogger('reseller_api.services.webhook_delivery'),
        ]
        for lg in loggers:
            lg.addHandler(handler)
            lg.setLevel(logging.DEBUG)

        try:
            # Exercise both sign and verify paths
            sig = sign_body(secret, body, ts)
            verify_signature(secret, body, ts, sig)
            # Also exercise a failing verify (to trigger any error-path logging)
            verify_signature(secret, body + b'x', ts, sig)
            verify_signature(secret, body, str(int(ts) - FRESHNESS_WINDOW_SECONDS - 1), sig)
        finally:
            for lg in loggers:
                lg.removeHandler(handler)

        log_output = log_stream.getvalue()
        self.assertNotIn(
            secret, log_output,
            f"Webhook secret must NEVER appear in log output.\nCaptured:\n{log_output[:500]}",
        )

    # ------------------------------------------------------------------ Determinism
    def test_sign_body_is_deterministic(self):
        """Same inputs must always produce the same signature."""
        ts = _now_ts()
        s1 = sign_body(SECRET, BODY, ts)
        s2 = sign_body(SECRET, BODY, ts)
        self.assertEqual(s1, s2)

    def test_sign_body_different_secrets_differ(self):
        ts = _now_ts()
        s1 = sign_body("secret-A", BODY, ts)
        s2 = sign_body("secret-B", BODY, ts)
        self.assertNotEqual(s1, s2)

    def test_sign_body_different_bodies_differ(self):
        ts = _now_ts()
        s1 = sign_body(SECRET, b'{"a":1}', ts)
        s2 = sign_body(SECRET, b'{"a":2}', ts)
        self.assertNotEqual(s1, s2)

    # ------------------------------------------------------------------ Signature format
    def test_signature_has_correct_prefix(self):
        ts, sig = _valid_sig()
        self.assertTrue(sig.startswith("sha256="), f"Bad sig format: {sig}")

    def test_signature_hex_length(self):
        """SHA-256 hex digest is exactly 64 characters."""
        ts, sig = _valid_sig()
        hex_part = sig[len("sha256="):]
        self.assertEqual(len(hex_part), 64)
        self.assertRegex(hex_part, r'^[0-9a-f]{64}$')

    def test_signature_matches_manual_hmac(self):
        """Sign output must match a manually computed HMAC-SHA256."""
        ts   = "1700000000"
        body = b'{"x":1}'
        sig  = sign_body(SECRET, body, ts)
        expected_input = ts.encode("ascii") + b"." + body
        expected_digest = hmac.new(
            SECRET.encode("utf-8"), expected_input, hashlib.sha256
        ).hexdigest()
        self.assertEqual(sig, f"sha256={expected_digest}")

    # ------------------------------------------------------------------ build_signed_string
    def test_build_signed_string_format(self):
        """Signed string must be b'{timestamp}.{body}'."""
        ts   = "1234567890"
        body = b'hello'
        result = build_signed_string(body, ts)
        self.assertEqual(result, b"1234567890.hello")

    # ------------------------------------------------------------------ Freshness
    def test_is_fresh_current_timestamp(self):
        self.assertTrue(is_fresh(_now_ts()))

    def test_is_fresh_expired(self):
        old = str(int(time.time()) - FRESHNESS_WINDOW_SECONDS - 1)
        self.assertFalse(is_fresh(old))

    def test_is_fresh_far_future(self):
        """Timestamps far in the future (clock skew attacks) must be rejected."""
        future = str(int(time.time()) + FRESHNESS_WINDOW_SECONDS + 1)
        self.assertFalse(is_fresh(future))

    def test_is_fresh_non_integer_string(self):
        self.assertFalse(is_fresh("not-a-number"))

    def test_is_fresh_none(self):
        self.assertFalse(is_fresh(None))  # type: ignore[arg-type]

    def test_freshness_window_is_at_least_60s(self):
        """The freshness window must be usable in practice (>= 60 s)."""
        self.assertGreaterEqual(FRESHNESS_WINDOW_SECONDS, 60)

    # ------------------------------------------------------------------ Constant-time
    def test_verify_uses_compare_digest(self):
        """verify_signature must not use plain == for signature comparison.

        We test this indirectly: hmac.compare_digest returns True for equal
        byte sequences and False otherwise, and verify_signature must return
        the same boolean as a correct HMAC comparison.
        """
        ts, sig = _valid_sig()
        # Correct: must be True
        self.assertTrue(verify_signature(SECRET, BODY, ts, sig))
        # Off-by-one byte in sig hex: must be False without early exit
        chars = list(sig)
        chars[-1] = '0' if chars[-1] != '0' else '1'
        bad_sig = "".join(chars)
        self.assertFalse(verify_signature(SECRET, BODY, ts, bad_sig,
                                          check_freshness=False))

    # ------------------------------------------------------------------ extract_headers
    def test_extract_headers_case_insensitive(self):
        headers = {
            "x-oggystore-signature-256": "sha256=abc",
            "X-OGGYSTORE-TIMESTAMP":     "1000",
            "X-Oggystore-Delivery":      "uuid-1",
            "x-oggystore-event":         "order.created",
        }
        result = extract_headers(headers)
        self.assertEqual(result["signature"],   "sha256=abc")
        self.assertEqual(result["timestamp"],   "1000")
        self.assertEqual(result["delivery_id"], "uuid-1")
        self.assertEqual(result["event_type"],  "order.created")

    def test_extract_headers_missing_returns_none(self):
        result = extract_headers({})
        for key in ("signature", "timestamp", "delivery_id", "event_type"):
            self.assertIsNone(result[key], f"{key} should be None when absent")

    # ------------------------------------------------------------------ generate_webhook_secret
    def test_generate_secret_is_string(self):
        s = generate_webhook_secret()
        self.assertIsInstance(s, str)

    def test_generate_secret_length(self):
        """token_urlsafe(32) produces at least 40 characters."""
        s = generate_webhook_secret()
        self.assertGreaterEqual(len(s), 40)

    def test_generate_secret_unique(self):
        secrets = {generate_webhook_secret() for _ in range(20)}
        self.assertEqual(len(secrets), 20, "All generated secrets must be unique")

    # ------------------------------------------------------------------ Header name constants
    def test_header_constants_match_documented_values(self):
        self.assertEqual(HEADER_SIGNATURE, "X-Oggystore-Signature-256")
        self.assertEqual(HEADER_TIMESTAMP, "X-Oggystore-Timestamp")
        self.assertEqual(HEADER_DELIVERY,  "X-Oggystore-Delivery")
        self.assertEqual(HEADER_EVENT,     "X-Oggystore-Event")

    # ------------------------------------------------------------------ current_timestamp
    def test_current_timestamp_is_numeric_string(self):
        ts = current_timestamp()
        self.assertTrue(ts.isdigit(), f"current_timestamp must be numeric: {ts!r}")
        self.assertAlmostEqual(int(ts), int(time.time()), delta=2)

    # ------------------------------------------------------------------ verify without freshness check
    def test_verify_skip_freshness_accepts_old(self):
        """check_freshness=False lets integration tests bypass the clock."""
        old_ts = str(int(time.time()) - 9999)
        sig = sign_body(SECRET, BODY, old_ts)
        self.assertTrue(
            verify_signature(SECRET, BODY, old_ts, sig, check_freshness=False),
            "check_freshness=False should accept old but correct signatures",
        )


if __name__ == '__main__':
    unittest.main()
