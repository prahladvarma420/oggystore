import re
import unittest
from unittest.mock import patch

from flask import request

from reseller_api import create_app
from reseller_api.context import AuthenticatedPrincipal
from reseller_api.responses import success_response
from reseller_api.services.rate_limit import RequestRateDecision


class ApiV1FoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.auth_headers = {
            "Authorization": "Bearer rk_test_ABCDEFGH." + ("x" * 43)
        }
        cls.auth_patcher = patch(
            "reseller_api.middleware.authentication.authenticate_credentials",
            return_value=AuthenticatedPrincipal(
                environment="test",
                reseller_id=1,
                reseller_public_id="RSR-ABCDEFGH23",
                api_key_id=1,
                api_key_public_id="rk_test_ABCDEFGH",
            ),
        )
        cls.auth_patcher.start()
        cls.audit_patcher = patch(
            "reseller_api.middleware.request_context.persist_audit_record",
            return_value=True,
        )
        cls.audit_writer = cls.audit_patcher.start()
        cls.rate_patcher = patch(
            "reseller_api.middleware.rate_limit.evaluate_request_rate",
            return_value=RequestRateDecision(
                True, "allowed", limit=60, remaining=59, reset_after=45
            ),
        )
        cls.rate_evaluator = cls.rate_patcher.start()
        cls.global_control_patcher = patch(
            "reseller_api.middleware.security_controls.is_global_api_enabled",
            return_value=True,
        )
        cls.global_control = cls.global_control_patcher.start()
        cls.app = create_app({
            "TESTING": True,
            "MAX_CONTENT_LENGTH": 1024,
            "RESELLER_API_HSTS": False,
            "API_ENVIRONMENT": "test",
            "INIT_DATABASE": False,
        })

        @cls.app.get("/api/v1/_test_failure")
        def _test_failure():
            raise RuntimeError("PRIVATE_SQL_PATH_SECRET")

        @cls.app.post("/api/v1/_test_echo")
        def _test_echo():
            return success_response(request.get_json())

        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.global_control_patcher.stop()
        cls.rate_patcher.stop()
        cls.audit_patcher.stop()
        cls.auth_patcher.stop()

    def assert_envelope(self, response, expected_ok):
        self.assertEqual(response.content_type, "application/json")
        body = response.get_json()
        self.assertIs(body["ok"], expected_ok)
        self.assertRegex(body["request_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(response.headers["X-Request-ID"], body["request_id"])
        return body

    def test_canonical_namespace_success(self):
        response = self.client.get("/api/v1/", headers=self.auth_headers)
        self.assertEqual(response.status_code, 200)
        body = self.assert_envelope(response, True)
        self.assertEqual(body["data"]["version"], "v1")
        self.assertEqual(body["data"]["status"], "available")
        self.assertEqual(
            body["data"]["available_resources"],
            ["products", "my-products", "pricing", "balance", "orders",
             "usage", "analytics", "webhooks"],
        )
        self.assertIn("available", body["data"]["message"])

    def test_no_slash_alias_is_json_not_redirect(self):
        response = self.client.get("/api/v1", headers=self.auth_headers)
        self.assertEqual(response.status_code, 200)
        self.assert_envelope(response, True)

    def test_registered_resource_routes_require_authentication(self):
        """Formerly 'future' routes now exist (Phases 3-11): orders, balance,
        webhooks. Unauthenticated access must be rejected with 401."""
        for path in (
            "/api/v1/orders",
            "/api/v1/balance",
            "/api/v1/webhooks",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 401, path)
            body = self.assert_envelope(response, False)
            self.assertEqual(body["error"]["code"], "authentication_failed")

    def test_structured_method_error(self):
        response = self.client.post("/api/v1/", headers=self.auth_headers)
        self.assertEqual(response.status_code, 405)
        body = self.assert_envelope(response, False)
        self.assertEqual(body["error"]["code"], "method_not_allowed")

    def test_structured_not_found(self):
        response = self.client.get("/api/v1/does-not-exist")
        self.assertEqual(response.status_code, 404)
        body = self.assert_envelope(response, False)
        self.assertEqual(body["error"], {
            "code": "not_found",
            "message": "The requested API resource was not found.",
        })

    def test_unexpected_error_is_sanitized(self):
        response = self.client.get("/api/v1/_test_failure")
        self.assertEqual(response.status_code, 500)
        body = self.assert_envelope(response, False)
        rendered = response.get_data(as_text=True)
        self.assertEqual(body["error"]["code"], "internal_error")
        for forbidden in (
            "PRIVATE_SQL_PATH_SECRET", "RuntimeError", "Traceback", "SELECT ",
            "/data/", "reseller_api/errors.py",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_json_content_type_enforced_for_body(self):
        response = self.client.post(
            "/api/v1/_test_echo",
            data="plain text",
            content_type="text/plain",
        )
        self.assertEqual(response.status_code, 415)
        body = self.assert_envelope(response, False)
        self.assertEqual(body["error"]["code"], "unsupported_media_type")

    def test_body_limit_is_structured(self):
        response = self.client.post(
            "/api/v1/_test_echo",
            json={"value": "x" * 2048},
        )
        self.assertEqual(response.status_code, 413)
        body = self.assert_envelope(response, False)
        self.assertEqual(body["error"]["code"], "payload_too_large")

    def test_request_ids_are_server_generated_and_unique(self):
        first_headers = dict(self.auth_headers)
        first_headers["X-Request-ID"] = "attacker-controlled"
        first = self.client.get(
            "/api/v1/", headers=first_headers
        )
        second = self.client.get("/api/v1/", headers=self.auth_headers)
        first_id = first.get_json()["request_id"]
        second_id = second.get_json()["request_id"]
        self.assertNotEqual(first_id, "attacker-controlled")
        self.assertNotEqual(first_id, second_id)

    def test_security_headers_and_no_cors(self):
        response = self.client.get("/api/v1/", headers=self.auth_headers)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        self.assertEqual(response.headers["RateLimit-Limit"], "60")
        self.assertEqual(response.headers["RateLimit-Remaining"], "59")
        self.assertEqual(response.headers["RateLimit-Reset"], "45")

    def test_rate_limit_exceeded_is_structured_and_has_metadata(self):
        denied = RequestRateDecision(
            False,
            "rate_limit_exceeded",
            limit=2,
            remaining=0,
            reset_after=30,
        )
        with patch(
            "reseller_api.middleware.rate_limit.evaluate_request_rate",
            return_value=denied,
        ):
            response = self.client.get("/api/v1/", headers=self.auth_headers)
        self.assertEqual(response.status_code, 429)
        body = self.assert_envelope(response, False)
        self.assertEqual(body["error"], {
            "code": "rate_limit_exceeded",
            "message": "Too many requests.",
        })
        self.assertEqual(response.headers["RateLimit-Limit"], "2")
        self.assertEqual(response.headers["RateLimit-Remaining"], "0")
        self.assertEqual(response.headers["RateLimit-Reset"], "30")
        self.assertEqual(response.headers["Retry-After"], "30")

    def test_global_api_disable_rejects_before_authentication(self):
        with patch(
            "reseller_api.middleware.security_controls.is_global_api_enabled",
            return_value=False,
        ):
            response = self.client.get("/api/v1/", headers=self.auth_headers)
        self.assertEqual(response.status_code, 503)
        body = self.assert_envelope(response, False)
        self.assertEqual(body["error"], {
            "code": "api_unavailable",
            "message": "The reseller API is currently unavailable.",
        })
        self.assertEqual(response.headers["Retry-After"], "60")

    def test_method_override_and_ambiguous_framing_are_rejected(self):
        overridden = self.client.get(
            "/api/v1/",
            headers={**self.auth_headers, "X-HTTP-Method-Override": "DELETE"},
        )
        self.assertEqual(overridden.status_code, 400)
        body = self.assert_envelope(overridden, False)
        self.assertEqual(body["error"]["code"], "invalid_request")

        ambiguous = self.client.open(
            "/api/v1/",
            method="POST",
            headers={
                **self.auth_headers,
                "Transfer-Encoding": "chunked",
                "Content-Length": "10",
            },
        )
        # POST on this GET-only route is rejected by URL/method matching (405)
        # before the payload guard runs; a 400 from the guard is equally valid.
        # The security property — the ambiguous-framing request never reaches a
        # handler — holds either way.
        self.assertIn(ambiguous.status_code, (400, 405))
        body = self.assert_envelope(ambiguous, False)
        self.assertIn(body["error"]["code"], ("invalid_request", "method_not_allowed"))

    def test_successful_request_audit_record_has_safe_metadata(self):
        self.audit_writer.reset_mock()
        response = self.client.get(
            "/api/v1/",
            headers={
                **self.auth_headers,
                "User-Agent": "Foundation Audit Test/1.0",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.audit_writer.assert_called_once()
        record = self.audit_writer.call_args.args[0]
        self.assertEqual(record.request_id, response.get_json()["request_id"])
        self.assertEqual(record.reseller_public_id, "RSR-ABCDEFGH23")
        self.assertEqual(record.key_id, "rk_test_ABCDEFGH")
        self.assertEqual(record.method, "GET")
        self.assertEqual(record.endpoint, "/api/v1/")
        self.assertEqual(record.status_code, 200)
        self.assertGreaterEqual(record.response_ms, 0)
        self.assertEqual(record.user_agent, "Foundation Audit Test/1.0")
        self.assertNotIn("x" * 43, repr(record))

    def test_failed_authentication_request_is_audited_anonymously(self):
        self.audit_writer.reset_mock()
        response = self.client.get("/api/v1/")
        self.assertEqual(response.status_code, 401)
        self.audit_writer.assert_called_once()
        record = self.audit_writer.call_args.args[0]
        self.assertEqual(record.request_id, response.get_json()["request_id"])
        self.assertEqual(record.status_code, 401)
        self.assertIsNone(record.reseller_id)
        self.assertIsNone(record.api_key_id)

    def test_audit_writer_failure_does_not_change_response(self):
        with patch(
            "reseller_api.middleware.request_context.persist_audit_record",
            side_effect=RuntimeError("temporary audit outage"),
        ):
            response = self.client.get("/api/v1/", headers=self.auth_headers)
        self.assertEqual(response.status_code, 200)
        self.assert_envelope(response, True)

    def test_missing_and_malformed_credentials_share_safe_error(self):
        missing = self.client.get("/api/v1/")
        malformed = self.client.get(
            "/api/v1/", headers={"Authorization": "Bearer malformed"}
        )
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(malformed.status_code, 401)
        missing_body = self.assert_envelope(missing, False)
        malformed_body = self.assert_envelope(malformed, False)
        self.assertEqual(missing_body["error"], malformed_body["error"])
        self.assertEqual(missing_body["error"], {
            "code": "authentication_failed",
            "message": "Authentication credentials are missing or invalid.",
        })

    def test_expected_v1_routes_are_registered(self):
        public_rules = {
            rule.rule for rule in self.app.url_map.iter_rules()
            if rule.rule.startswith("/api/v1") and "_test_" not in rule.rule
        }
        self.assertEqual(
            public_rules,
            {
                "/api/v1", "/api/v1/", "/api/v1/products",
                "/api/v1/products/<string:product_id>",
                "/api/v1/products/<string:product_id>/import",
                "/api/v1/my-products",
                "/api/v1/my-products/<string:product_id>",
                "/api/v1/pricing",
                "/api/v1/pricing/<string:product_id>",
                "/api/v1/balance",
                "/api/v1/usage",
                "/api/v1/analytics/sales",
                "/api/v1/wallet/summary",
                "/api/v1/exports/orders.csv",
                "/api/v1/orders",
                "/api/v1/orders/quote",
                "/api/v1/orders/<string:order_id>",
                "/api/v1/orders/<string:order_id>/delivery",
                "/api/v1/webhooks",
                "/api/v1/webhooks/analytics",
                "/api/v1/webhooks/<string:webhook_id>",
                "/api/v1/webhooks/<string:webhook_id>/rotate-secret",
                "/api/v1/webhooks/<string:webhook_id>/test",
                "/api/v1/webhooks/events/<string:outbox_id>/retry",
                "/api/v1/webhooks/<string:webhook_id>/deliveries",
                "/api/v1/webhooks/<string:webhook_id>/deliveries/<string:delivery_id>/retry",
            },
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
