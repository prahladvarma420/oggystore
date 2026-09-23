import re
import unittest

from flask import request

from reseller_api import create_app
from reseller_api.responses import success_response


class ApiV1FoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app({
            "TESTING": True,
            "MAX_CONTENT_LENGTH": 1024,
            "RESELLER_API_HSTS": False,
        })

        @cls.app.get("/api/v1/_test_failure")
        def _test_failure():
            raise RuntimeError("PRIVATE_SQL_PATH_SECRET")

        @cls.app.post("/api/v1/_test_echo")
        def _test_echo():
            return success_response(request.get_json())

        cls.client = cls.app.test_client()

    def assert_envelope(self, response, expected_ok):
        self.assertEqual(response.content_type, "application/json")
        body = response.get_json()
        self.assertIs(body["ok"], expected_ok)
        self.assertRegex(body["request_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(response.headers["X-Request-ID"], body["request_id"])
        return body

    def test_canonical_namespace_success(self):
        response = self.client.get("/api/v1/")
        self.assertEqual(response.status_code, 200)
        body = self.assert_envelope(response, True)
        self.assertEqual(body["data"]["version"], "v1")
        self.assertEqual(body["data"]["status"], "foundation")
        self.assertEqual(body["data"]["available_resources"], [])
        self.assertIn("not available", body["data"]["message"])

    def test_no_slash_alias_is_json_not_redirect(self):
        response = self.client.get("/api/v1")
        self.assertEqual(response.status_code, 200)
        self.assert_envelope(response, True)

    def test_future_resource_routes_do_not_exist(self):
        for path in (
            "/api/v1/products",
            "/api/v1/orders",
            "/api/v1/balance",
            "/api/v1/webhooks",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404, path)
            body = self.assert_envelope(response, False)
            self.assertEqual(body["error"]["code"], "not_found")

    def test_structured_method_error(self):
        response = self.client.post("/api/v1/")
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
        first = self.client.get(
            "/api/v1/", headers={"X-Request-ID": "attacker-controlled"}
        )
        second = self.client.get("/api/v1/")
        first_id = first.get_json()["request_id"]
        second_id = second.get_json()["request_id"]
        self.assertNotEqual(first_id, "attacker-controlled")
        self.assertNotEqual(first_id, second_id)

    def test_security_headers_and_no_cors(self):
        response = self.client.get("/api/v1/")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_only_foundation_routes_are_registered(self):
        public_rules = {
            rule.rule for rule in self.app.url_map.iter_rules()
            if rule.rule.startswith("/api/v1") and "_test_" not in rule.rule
        }
        self.assertEqual(public_rules, {"/api/v1", "/api/v1/"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
