"""Comprehensive test suite for POST/GET/PATCH/DELETE /api/v1/webhooks.

Phase 9 Prompt 2 — Webhook Configuration Layer.

Covers:
  - create, list, retrieve, update, delete (happy paths)
  - invalid URL, unsupported scheme, HTTP (not HTTPS), malformed
  - invalid event type, empty events list
  - IDOR (webhook owned by reseller-2 accessed as reseller-1)
  - suspended reseller → 403
  - revoked API key → 401
  - secret never returned on GET / PATCH / LIST
  - secret returned exactly once on POST 201
  - invalid webhook_id format → 404
  - unknown patch field → 400
  - secret patch attempt → 400
  - webhook limit (max 5)
  - duplicate URL → 409
"""

import importlib.util
import json
import unittest
from unittest.mock import MagicMock, patch

_FLASK_AVAILABLE = importlib.util.find_spec("flask") is not None


if not _FLASK_AVAILABLE:
    class WebhookApiTests(unittest.TestCase):
        @unittest.skip("Flask is not installed in this validation environment")
        def test_requires_flask(self):
            pass
else:
    from reseller_api import create_app
    from reseller_api.context import AuthenticatedPrincipal
    from reseller_api.services.rate_limit import RequestRateDecision
    from reseller_api.services.webhook_config import WEBHOOK_EVENT_TYPES

    # ------------------------------------------------------------------
    # Shared test fixture helpers
    # ------------------------------------------------------------------

    def _make_principal(
        reseller_id=1,
        public_id="RSR-0000000001",
        key_public_id="rk_test_WEBHAAKK",
    ):
        return AuthenticatedPrincipal(
            environment="test",
            reseller_id=reseller_id,
            reseller_public_id=public_id,
            api_key_id=reseller_id,
            api_key_public_id=key_public_id,
        )

    def _make_rate_ok():
        return RequestRateDecision(True, "allowed", limit=60, remaining=59, reset_after=45)

    def _make_hook_row(
        hook_id=1,
        reseller_id=1,
        url="https://example.com/hook",
        enabled=1,
        events=None,
    ):
        """Minimal dict matching what reseller_db helpers return."""
        import json as _json
        return {
            "id":      hook_id,
            "reseller_id": reseller_id,
            "url":     url,
            "enabled": enabled,
            "events":  events or _json.dumps(sorted(WEBHOOK_EVENT_TYPES)),
            "created": "2026-09-29 18:00:00",
            "updated": "2026-09-29 18:00:00",
        }

    # ------------------------------------------------------------------
    # Main test class
    # ------------------------------------------------------------------

    class WebhookApiTests(unittest.TestCase):

        @classmethod
        def setUpClass(cls):
            cls.principal = _make_principal()

            cls.auth_patcher = patch(
                "reseller_api.middleware.authentication.authenticate_credentials",
                return_value=cls.principal,
            )
            cls.auth_patcher.start()

            cls.audit_patcher = patch(
                "reseller_api.middleware.request_context.persist_audit_record",
                return_value=True,
            )
            cls.audit_patcher.start()

            cls.rate_patcher = patch(
                "reseller_api.middleware.rate_limit.evaluate_request_rate",
                return_value=_make_rate_ok(),
            )
            cls.rate_patcher.start()

            cls.global_ctrl_patcher = patch(
                "reseller_api.middleware.security_controls.is_global_api_enabled",
                return_value=True,
            )
            cls.global_ctrl_patcher.start()

            # authorize_operation always grants by default; override per-test
            # when testing suspension/revocation.
            cls.authz_patcher = patch(
                "reseller_api.middleware.authorization.authorize_operation",
                return_value=MagicMock(allowed=True, reason="ok"),
            )
            cls.authz_patcher.start()

            cls.app = create_app({
                "TESTING": True,
                "MAX_CONTENT_LENGTH": 1024 * 64,
                "RESELLER_API_HSTS": False,
                "API_ENVIRONMENT": "test",
                "INIT_DATABASE": False,
            })
            cls.client = cls.app.test_client()

        @classmethod
        def tearDownClass(cls):
            cls.auth_patcher.stop()
            cls.audit_patcher.stop()
            cls.rate_patcher.stop()
            cls.global_ctrl_patcher.stop()
            cls.authz_patcher.stop()

        def _auth_headers(self):
            return {
                "Authorization": "Bearer rk_test_WEBHAAKK." + ("s" * 43),
                "Content-Type":  "application/json",
            }

        def _post(self, body):
            return self.client.post(
                "/api/v1/webhooks",
                data=json.dumps(body),
                headers=self._auth_headers(),
            )

        def _get_list(self):
            return self.client.get(
                "/api/v1/webhooks",
                headers=self._auth_headers(),
            )

        def _get_one(self, wid):
            return self.client.get(
                f"/api/v1/webhooks/{wid}",
                headers=self._auth_headers(),
            )

        def _patch(self, wid, body):
            return self.client.patch(
                f"/api/v1/webhooks/{wid}",
                data=json.dumps(body),
                headers=self._auth_headers(),
            )

        def _delete(self, wid):
            return self.client.delete(
                f"/api/v1/webhooks/{wid}",
                headers=self._auth_headers(),
            )

        # ----------------------------------------------------------------
        # CREATE — happy path
        # ----------------------------------------------------------------

        def test_create_returns_201_with_secret(self):
            """POST /api/v1/webhooks creates a webhook and returns secret once."""
            hook_row = _make_hook_row()
            with (
                patch("reseller_api.services.webhook_config.rdb.list_webhooks", return_value=[]),
                patch("reseller_api.services.webhook_config.rdb.validate_webhook_url",
                      return_value="https://example.com/hook"),
                patch("reseller_api.services.webhook_config.rdb.create_webhook",
                      return_value=(hook_row, "plaintext_secret_abc123")),
                patch("reseller_api.services.webhook_config._set_events"),
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=hook_row),
                patch("reseller_api.services.webhook_config.rdb.log_security_event"),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(["order.created", "order.failed"])),
            ):
                resp = self._post({"url": "https://example.com/hook"})

            self.assertEqual(201, resp.status_code)
            data = resp.get_json()
            self.assertTrue(data["ok"])
            hook = data["data"]["webhook"]
            self.assertIn("secret", hook)
            self.assertEqual("plaintext_secret_abc123", hook["secret"])
            self.assertIn("secret_note", hook)
            self.assertTrue(hook["webhook_id"].startswith("WHK-"))
            self.assertIn("supported_events", data["data"])

        def test_create_default_events_all_types(self):
            """When events not supplied, all supported types are subscribed."""
            hook_row = _make_hook_row()
            with (
                patch("reseller_api.services.webhook_config.rdb.list_webhooks", return_value=[]),
                patch("reseller_api.services.webhook_config.rdb.validate_webhook_url",
                      return_value="https://example.com/hook"),
                patch("reseller_api.services.webhook_config.rdb.create_webhook",
                      return_value=(hook_row, "secret")),
                patch("reseller_api.services.webhook_config._set_events"),
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=hook_row),
                patch("reseller_api.services.webhook_config.rdb.log_security_event"),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(sorted(WEBHOOK_EVENT_TYPES))),
            ):
                resp = self._post({"url": "https://example.com/hook"})

            self.assertEqual(201, resp.status_code)
            hook = resp.get_json()["data"]["webhook"]
            for ev in WEBHOOK_EVENT_TYPES:
                self.assertIn(ev, hook["events"])

        def test_create_with_specific_events(self):
            """POST with a valid events subset stores only those events."""
            chosen = ["order.created", "refund.created"]
            hook_row = _make_hook_row(events=json.dumps(chosen))
            with (
                patch("reseller_api.services.webhook_config.rdb.list_webhooks", return_value=[]),
                patch("reseller_api.services.webhook_config.rdb.validate_webhook_url",
                      return_value="https://example.com/hook"),
                patch("reseller_api.services.webhook_config.rdb.create_webhook",
                      return_value=(hook_row, "secret")),
                patch("reseller_api.services.webhook_config._set_events"),
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=hook_row),
                patch("reseller_api.services.webhook_config.rdb.log_security_event"),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(chosen)),
            ):
                resp = self._post({"url": "https://example.com/hook", "events": chosen})

            self.assertEqual(201, resp.status_code)
            hook = resp.get_json()["data"]["webhook"]
            self.assertEqual(sorted(chosen), hook["events"])

        # ----------------------------------------------------------------
        # CREATE — validation failures
        # ----------------------------------------------------------------

        def test_create_missing_url_returns_400(self):
            resp = self._post({"enabled": True})
            self.assertEqual(400, resp.status_code)
            self.assertFalse(resp.get_json()["ok"])

        def test_create_non_https_url_returns_400(self):
            with (
                patch("reseller_api.services.webhook_config.rdb.list_webhooks", return_value=[]),
                patch("reseller_api.services.webhook_config.rdb.validate_webhook_url",
                      side_effect=ValueError("Only HTTPS URLs are allowed.")),
            ):
                resp = self._post({"url": "http://example.com/hook"})
            self.assertEqual(400, resp.status_code)
            self.assertEqual("invalid_webhook_url", resp.get_json()["error"]["code"])

        def test_create_localhost_url_blocked(self):
            """SSRF: localhost URLs must be rejected."""
            with (
                patch("reseller_api.services.webhook_config.rdb.list_webhooks", return_value=[]),
                patch("reseller_api.services.webhook_config.rdb.validate_webhook_url",
                      side_effect=ValueError("Private/loopback destinations are not allowed.")),
            ):
                resp = self._post({"url": "https://localhost/hook"})
            self.assertEqual(400, resp.status_code)
            self.assertEqual("invalid_webhook_url", resp.get_json()["error"]["code"])

        def test_create_private_ip_blocked(self):
            """SSRF: private IP destinations must be rejected."""
            with (
                patch("reseller_api.services.webhook_config.rdb.list_webhooks", return_value=[]),
                patch("reseller_api.services.webhook_config.rdb.validate_webhook_url",
                      side_effect=ValueError("Private IP ranges are not allowed.")),
            ):
                resp = self._post({"url": "https://192.168.1.1/hook"})
            self.assertEqual(400, resp.status_code)

        def test_create_unsupported_event_returns_400(self):
            """Unknown event type names must be rejected."""
            with (
                patch("reseller_api.services.webhook_config.rdb.list_webhooks", return_value=[]),
                patch("reseller_api.services.webhook_config.rdb.validate_webhook_url",
                      return_value="https://example.com/hook"),
            ):
                resp = self._post({
                    "url":    "https://example.com/hook",
                    "events": ["payment.success"],   # not in WEBHOOK_EVENT_TYPES
                })
            self.assertEqual(400, resp.status_code)
            self.assertEqual("unsupported_event_type", resp.get_json()["error"]["code"])

        def test_create_events_not_a_list_returns_400(self):
            with (
                patch("reseller_api.services.webhook_config.rdb.list_webhooks", return_value=[]),
                patch("reseller_api.services.webhook_config.rdb.validate_webhook_url",
                      return_value="https://example.com/hook"),
            ):
                resp = self._post({
                    "url":    "https://example.com/hook",
                    "events": "order.created",     # string, not list
                })
            self.assertEqual(400, resp.status_code)
            self.assertEqual("invalid_events", resp.get_json()["error"]["code"])

        def test_create_duplicate_url_returns_409(self):
            hook_row = _make_hook_row()
            with (
                patch("reseller_api.services.webhook_config.rdb.list_webhooks", return_value=[]),
                patch("reseller_api.services.webhook_config.rdb.validate_webhook_url",
                      return_value="https://example.com/hook"),
                patch("reseller_api.services.webhook_config.rdb.create_webhook",
                      side_effect=Exception("UNIQUE constraint failed")),
            ):
                resp = self._post({"url": "https://example.com/hook"})
            self.assertEqual(409, resp.status_code)
            self.assertEqual("webhook_url_conflict", resp.get_json()["error"]["code"])

        def test_create_limit_returns_422(self):
            """Cannot create a 6th webhook."""
            existing = [_make_hook_row(hook_id=i) for i in range(1, 6)]
            with patch("reseller_api.services.webhook_config.rdb.list_webhooks",
                       return_value=existing):
                resp = self._post({"url": "https://example.com/hook"})
            self.assertEqual(422, resp.status_code)
            self.assertEqual("webhook_limit_reached", resp.get_json()["error"]["code"])

        def test_create_no_json_body_returns_400(self):
            resp = self.client.post(
                "/api/v1/webhooks",
                data="not json",
                headers={
                    "Authorization": "Bearer rk_test_WEBHAAKK." + "s" * 43,
                    "Content-Type":  "text/plain",
                },
            )
            self.assertIn(resp.status_code, (400, 415))

        # ----------------------------------------------------------------
        # LIST — happy path + secret absence
        # ----------------------------------------------------------------

        def test_list_returns_webhooks(self):
            hooks = [_make_hook_row(hook_id=1), _make_hook_row(hook_id=2, url="https://other.com/")]
            with (
                patch("reseller_api.services.webhook_config.rdb.list_webhooks",
                      return_value=hooks),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(["order.created"])),
            ):
                resp = self._get_list()

            self.assertEqual(200, resp.status_code)
            data = resp.get_json()["data"]
            self.assertEqual(2, data["count"])
            self.assertEqual(2, len(data["webhooks"]))

        def test_list_never_returns_secret(self):
            hooks = [_make_hook_row()]
            with (
                patch("reseller_api.services.webhook_config.rdb.list_webhooks",
                      return_value=hooks),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(["order.created"])),
            ):
                resp = self._get_list()

            for hook in resp.get_json()["data"]["webhooks"]:
                self.assertNotIn("secret", hook)
                self.assertNotIn("secret_encrypted", hook)
                self.assertNotIn("secret_note", hook)

        def test_list_empty(self):
            with patch("reseller_api.services.webhook_config.rdb.list_webhooks", return_value=[]):
                resp = self._get_list()
            self.assertEqual(200, resp.status_code)
            self.assertEqual(0, resp.get_json()["data"]["count"])

        # ----------------------------------------------------------------
        # RETRIEVE single webhook
        # ----------------------------------------------------------------

        def test_get_webhook_returns_200(self):
            hook_row = _make_hook_row(hook_id=42)
            with (
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=hook_row),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(["order.created"])),
            ):
                resp = self._get_one("WHK-00000042")

            self.assertEqual(200, resp.status_code)
            hook = resp.get_json()["data"]["webhook"]
            self.assertEqual("WHK-00000042", hook["webhook_id"])
            self.assertNotIn("secret", hook)

        def test_get_invalid_id_format_returns_404(self):
            """A non-WHK-XXXXXXXX string must return 404, not 500."""
            resp = self._get_one("INVALID-ID")
            self.assertEqual(404, resp.status_code)

        def test_get_missing_webhook_returns_404(self):
            with patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                       return_value=None):
                resp = self._get_one("WHK-00009999")
            self.assertEqual(404, resp.status_code)
            self.assertEqual("webhook_not_found", resp.get_json()["error"]["code"])

        def test_get_another_resellers_webhook_returns_404(self):
            """IDOR: DB query scoped by reseller_id must return None for foreign IDs."""
            # The DB helper returns None because it does AND reseller_id=<auth_context>.
            with patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                       return_value=None):
                resp = self._get_one("WHK-00000002")
            self.assertEqual(404, resp.status_code)  # not 403 — indistinguishable

        # ----------------------------------------------------------------
        # UPDATE (PATCH)
        # ----------------------------------------------------------------

        def test_patch_url_returns_200(self):
            new_url = "https://updated.example.com/hook"
            hook_row = _make_hook_row(url=new_url)
            with (
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=hook_row),
                patch("reseller_api.services.webhook_config.rdb.validate_webhook_url",
                      return_value=new_url),
                patch("reseller_api.services.webhook_config.rdb.update_webhook_url",
                      return_value=True),
                patch("reseller_api.services.webhook_config.rdb.log_security_event"),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(["order.created"])),
            ):
                resp = self._patch("WHK-00000001", {"url": new_url})

            self.assertEqual(200, resp.status_code)
            hook = resp.get_json()["data"]["webhook"]
            self.assertEqual(new_url, hook["url"])
            self.assertNotIn("secret", hook)

        def test_patch_enabled_false(self):
            hook_row = _make_hook_row(enabled=0)
            with (
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=hook_row),
                patch("reseller_api.services.webhook_config.rdb.set_webhook_enabled_for_reseller"),
                patch("reseller_api.services.webhook_config.rdb.log_security_event"),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(["order.created"])),
            ):
                resp = self._patch("WHK-00000001", {"enabled": False})

            self.assertEqual(200, resp.status_code)

        def test_patch_events_subset(self):
            chosen = ["order.failed", "refund.created"]
            hook_row = _make_hook_row(events=json.dumps(chosen))
            with (
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=hook_row),
                patch("reseller_api.services.webhook_config._set_events"),
                patch("reseller_api.services.webhook_config.rdb.log_security_event"),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(sorted(chosen))),
            ):
                resp = self._patch("WHK-00000001", {"events": chosen})

            self.assertEqual(200, resp.status_code)

        def test_patch_secret_attempt_returns_400(self):
            """Attempting to PATCH the secret must be explicitly rejected."""
            resp = self._patch("WHK-00000001", {"secret": "new_secret"})
            self.assertEqual(400, resp.status_code)
            self.assertEqual("secret_not_patchable", resp.get_json()["error"]["code"])

        def test_patch_unknown_field_returns_400(self):
            resp = self._patch("WHK-00000001", {"reseller_id": 999})
            self.assertEqual(400, resp.status_code)
            self.assertEqual("unknown_fields", resp.get_json()["error"]["code"])

        def test_patch_no_fields_returns_400(self):
            resp = self._patch("WHK-00000001", {})
            self.assertEqual(400, resp.status_code)
            self.assertEqual("no_fields_provided", resp.get_json()["error"]["code"])

        def test_patch_invalid_event_returns_400(self):
            hook_row = _make_hook_row()
            with (
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=hook_row),
            ):
                resp = self._patch("WHK-00000001", {"events": ["not.real.event"]})
            self.assertEqual(400, resp.status_code)
            self.assertEqual("unsupported_event_type", resp.get_json()["error"]["code"])

        def test_patch_invalid_enabled_type_returns_400(self):
            hook_row = _make_hook_row()
            with (
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=hook_row),
            ):
                resp = self._patch("WHK-00000001", {"enabled": "yes"})
            self.assertEqual(400, resp.status_code)
            self.assertEqual("invalid_enabled", resp.get_json()["error"]["code"])

        def test_patch_missing_webhook_returns_404(self):
            with patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                       return_value=None):
                resp = self._patch("WHK-00000099", {"enabled": False})
            self.assertEqual(404, resp.status_code)

        def test_patch_another_resellers_webhook_returns_404(self):
            """IDOR on PATCH: foreign webhook returns 404."""
            with patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                       return_value=None):
                resp = self._patch("WHK-00000002", {"enabled": False})
            self.assertEqual(404, resp.status_code)

        def test_patch_invalid_id_format_returns_404(self):
            resp = self._patch("BAD_ID", {"enabled": False})
            self.assertEqual(404, resp.status_code)

        # ----------------------------------------------------------------
        # DELETE
        # ----------------------------------------------------------------

        def test_delete_returns_204(self):
            with (
                patch("reseller_api.services.webhook_config.rdb.delete_webhook_for_reseller",
                      return_value=True),
                patch("reseller_api.services.webhook_config.rdb.log_security_event"),
            ):
                resp = self._delete("WHK-00000001")
            self.assertEqual(204, resp.status_code)
            self.assertEqual(b"", resp.data)

        def test_delete_missing_webhook_returns_404(self):
            with patch("reseller_api.services.webhook_config.rdb.delete_webhook_for_reseller",
                       return_value=False):
                resp = self._delete("WHK-00009999")
            self.assertEqual(404, resp.status_code)
            self.assertEqual("webhook_not_found", resp.get_json()["error"]["code"])

        def test_delete_another_resellers_webhook_returns_404(self):
            """IDOR on DELETE: DB helper returns False for cross-reseller IDs."""
            with patch("reseller_api.services.webhook_config.rdb.delete_webhook_for_reseller",
                       return_value=False):
                resp = self._delete("WHK-00000002")
            self.assertEqual(404, resp.status_code)

        def test_delete_invalid_id_format_returns_404(self):
            resp = self._delete("webhook-123")
            self.assertEqual(404, resp.status_code)

        # ----------------------------------------------------------------
        # Secret exposure tests
        # ----------------------------------------------------------------

        def test_secret_never_in_list_response(self):
            hooks = [_make_hook_row()]
            with (
                patch("reseller_api.services.webhook_config.rdb.list_webhooks",
                      return_value=hooks),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(["order.created"])),
            ):
                resp = self._get_list()
            raw = resp.get_data(as_text=True)
            # The encrypted secret must not appear; neither must raw secret fields.
            self.assertNotIn("secret_encrypted", raw)
            self.assertNotIn("secret_note", raw)  # secret_note only appears on POST 201

        def test_secret_never_in_get_response(self):
            hook_row = _make_hook_row()
            with (
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=hook_row),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(["order.created"])),
            ):
                resp = self._get_one("WHK-00000001")
            raw = resp.get_data(as_text=True)
            self.assertNotIn("secret_encrypted", raw)
            self.assertNotIn("secret_note", raw)

        def test_secret_never_in_patch_response(self):
            hook_row = _make_hook_row()
            with (
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=hook_row),
                patch("reseller_api.services.webhook_config.rdb.set_webhook_enabled_for_reseller"),
                patch("reseller_api.services.webhook_config.rdb.log_security_event"),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(["order.created"])),
            ):
                resp = self._patch("WHK-00000001", {"enabled": True})
            raw = resp.get_data(as_text=True)
            self.assertNotIn("secret_encrypted", raw)
            self.assertNotIn("secret_note", raw)

        def test_secret_present_only_on_create_201(self):
            """Secret appears in the response body exactly once — at creation."""
            hook_row = _make_hook_row()
            with (
                patch("reseller_api.services.webhook_config.rdb.list_webhooks", return_value=[]),
                patch("reseller_api.services.webhook_config.rdb.validate_webhook_url",
                      return_value="https://example.com/hook"),
                patch("reseller_api.services.webhook_config.rdb.create_webhook",
                      return_value=(hook_row, "only_once_secret")),
                patch("reseller_api.services.webhook_config._set_events"),
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=hook_row),
                patch("reseller_api.services.webhook_config.rdb.log_security_event"),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(["order.created"])),
            ):
                resp = self._post({"url": "https://example.com/hook"})

            self.assertEqual(201, resp.status_code)
            body = resp.get_json()
            self.assertIn("secret", body["data"]["webhook"])
            self.assertEqual("only_once_secret", body["data"]["webhook"]["secret"])

        # ----------------------------------------------------------------
        # Authorization failures
        # ----------------------------------------------------------------

        def test_suspended_reseller_returns_403(self):
            """When authorize_operation returns allowed=False, expect 403."""
            denied = MagicMock(allowed=False, reason="reseller_suspended")
            with patch("reseller_api.middleware.authorization.authorize_operation",
                       return_value=denied):
                resp = self._get_list()
            self.assertEqual(403, resp.status_code)
            self.assertEqual("authorization_denied", resp.get_json()["error"]["code"])

        def test_revoked_api_key_returns_401(self):
            """When authentication returns None (revoked key), expect 401."""
            with patch("reseller_api.middleware.authentication.authenticate_credentials",
                       return_value=None):
                resp = self._get_list()
            self.assertEqual(401, resp.status_code)

        def test_missing_auth_header_returns_401(self):
            resp = self.client.get("/api/v1/webhooks")
            self.assertEqual(401, resp.status_code)

        # ----------------------------------------------------------------
        # Public ID format
        # ----------------------------------------------------------------

        def test_webhook_id_format(self):
            """Returned webhook_id must match WHK-XXXXXXXX (8 digits)."""
            import re
            hook_row = _make_hook_row(hook_id=7)
            with (
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=hook_row),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(["order.created"])),
            ):
                resp = self._get_one("WHK-00000007")
            hook = resp.get_json()["data"]["webhook"]
            self.assertRegex(hook["webhook_id"], r"^WHK-\d{8}$")

        # ----------------------------------------------------------------
        # Events whitelist
        # ----------------------------------------------------------------

        def test_all_valid_event_types_accepted(self):
            """Every WEBHOOK_EVENT_TYPES entry must pass validation."""
            hook_row = _make_hook_row()
            for ev in WEBHOOK_EVENT_TYPES:
                with (
                    patch("reseller_api.services.webhook_config.rdb.list_webhooks", return_value=[]),
                    patch("reseller_api.services.webhook_config.rdb.validate_webhook_url",
                          return_value="https://example.com/hook"),
                    patch("reseller_api.services.webhook_config.rdb.create_webhook",
                          return_value=(hook_row, "s")),
                    patch("reseller_api.services.webhook_config._set_events"),
                    patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                          return_value=hook_row),
                    patch("reseller_api.services.webhook_config.rdb.log_security_event"),
                    patch("reseller_api.services.webhook_config._get_events",
                          return_value=json.dumps([ev])),
                ):
                    resp = self._post({"url": "https://example.com/hook", "events": [ev]})
                self.assertEqual(201, resp.status_code, msg=f"Event type '{ev}' was rejected")

        def test_arbitrary_event_name_rejected(self):
            for bad in ["payment.success", "user.login", "*", "", "order"]:
                with (
                    patch("reseller_api.services.webhook_config.rdb.list_webhooks", return_value=[]),
                    patch("reseller_api.services.webhook_config.rdb.validate_webhook_url",
                          return_value="https://example.com/hook"),
                ):
                    resp = self._post({
                        "url":    "https://example.com/hook",
                        "events": [bad],
                    })
                self.assertIn(
                    resp.status_code, (400,),
                    msg=f"Event '{bad}' should have been rejected (got {resp.status_code})"
                )


if __name__ == "__main__":
    unittest.main()
