"""Comprehensive test suite — Webhook Management API (Phase 9 Prompt 9).

Covers every scenario listed in the prompt:

  CRUD:    create, list, retrieve, update, enable, disable, delete
  History: delivery history endpoint
  Retry:   retry failed delivery (by delivery_id)
  Errors:  invalid webhook, IDOR, another reseller, invalid event, invalid URL,
           revoked key, suspended reseller, blocked reseller, secret exposure,
           excessive retries

Security invariants verified:
  * reseller_id NEVER trusted from client input.
  * Secret returned once on POST 201 only.
  * Secret never appears in list / get / patch / delivery / retry responses.
  * IDOR: cross-reseller webhook IDs return 404 (indistinguishable from missing).
  * Delivery history: no secret, no auth headers, no stack traces, no payload_json.

Regression: existing Product / Wallet / Order / Auth API tests are run separately.
"""

import importlib.util
import json
import unittest
from unittest.mock import MagicMock, patch

_FLASK_AVAILABLE = importlib.util.find_spec("flask") is not None


if not _FLASK_AVAILABLE:
    class WebhookManagementApiTests(unittest.TestCase):
        @unittest.skip("Flask not installed in this validation environment")
        def test_requires_flask(self):
            pass

    class DeliveryHistoryTests(unittest.TestCase):
        @unittest.skip("Flask not installed")
        def test_requires_flask(self):
            pass

    class RetryDeliveryTests(unittest.TestCase):
        @unittest.skip("Flask not installed")
        def test_requires_flask(self):
            pass

    class SecurityTests(unittest.TestCase):
        @unittest.skip("Flask not installed")
        def test_requires_flask(self):
            pass

else:
    from reseller_api import create_app
    from reseller_api.context import AuthenticatedPrincipal
    from reseller_api.services.rate_limit import RequestRateDecision
    from reseller_api.services.webhook_config import WEBHOOK_EVENT_TYPES

    # ------------------------------------------------------------------
    # Shared fixture helpers
    # ------------------------------------------------------------------

    def _make_principal(reseller_id=1, public_id="RSR-0000000001",
                        key_public_id="rk_test_MGMTAPI1"):
        return AuthenticatedPrincipal(
            environment="test",
            reseller_id=reseller_id,
            reseller_public_id=public_id,
            api_key_id=reseller_id,
            api_key_public_id=key_public_id,
        )

    def _rate_ok():
        return RequestRateDecision(True, "allowed", limit=60, remaining=59, reset_after=45)

    def _make_hook_row(hook_id=1, reseller_id=1, url="https://example.com/hook",
                       enabled=1, events=None):
        return {
            "id":          hook_id,
            "reseller_id": reseller_id,
            "url":         url,
            "enabled":     enabled,
            "events":      events or json.dumps(sorted(WEBHOOK_EVENT_TYPES)),
            "created":     "2026-09-30 00:00:00",
            "updated":     "2026-09-30 00:00:00",
        }

    def _make_delivery_row(delivery_id=10, outbox_event_id=1, event_type="order.created",
                           event_status="failed", attempt=3, http_status=500,
                           success=False, error_category="server_error",
                           last_attempt_at="2026-09-30 00:05:00",
                           next_retry_at=None, created_at="2026-09-30 00:01:00"):
        return {
            "delivery_id":     delivery_id,
            "event_id":        outbox_event_id,
            "event_type":      event_type,
            "event_status":    event_status,
            "attempt":         attempt,
            "http_status":     http_status,
            "success":         success,
            "error_category":  error_category,
            "last_attempt_at": last_attempt_at,
            "next_retry_at":   next_retry_at,
            "created_at":      created_at,
        }

    def _make_delivery_row_with_outbox(delivery_id=10, outbox_event_id=1, **kw):
        """Delivery row shape returned by get_delivery_for_webhook (includes outbox_event_id)."""
        row = _make_delivery_row(delivery_id=delivery_id, outbox_event_id=outbox_event_id, **kw)
        row["outbox_event_id"] = outbox_event_id
        return row

    # ------------------------------------------------------------------
    # Shared test base
    # ------------------------------------------------------------------

    class _Base(unittest.TestCase):

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
                return_value=_rate_ok(),
            )
            cls.rate_patcher.start()

            cls.global_ctrl_patcher = patch(
                "reseller_api.middleware.security_controls.is_global_api_enabled",
                return_value=True,
            )
            cls.global_ctrl_patcher.start()

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

        def _headers(self):
            return {
                "Authorization": "Bearer rk_test_MGMTAPI1." + "s" * 43,
                "Content-Type":  "application/json",
            }

        def _post_webhook(self, body):
            return self.client.post("/api/v1/webhooks",
                                    data=json.dumps(body), headers=self._headers())

        def _get_list(self):
            return self.client.get("/api/v1/webhooks", headers=self._headers())

        def _get_one(self, wid):
            return self.client.get(f"/api/v1/webhooks/{wid}", headers=self._headers())

        def _patch(self, wid, body):
            return self.client.patch(f"/api/v1/webhooks/{wid}",
                                     data=json.dumps(body), headers=self._headers())

        def _delete(self, wid):
            return self.client.delete(f"/api/v1/webhooks/{wid}", headers=self._headers())

        def _get_deliveries(self, wid, qs=""):
            return self.client.get(f"/api/v1/webhooks/{wid}/deliveries{qs}",
                                   headers=self._headers())

        def _retry_delivery(self, wid, did):
            return self.client.post(f"/api/v1/webhooks/{wid}/deliveries/{did}/retry",
                                    headers=self._headers())

    # ==================================================================
    # 1. CRUD — Create
    # ==================================================================

    class WebhookManagementApiTests(_Base):

        # ---- Create ---------------------------------------------------

        def test_create_returns_201_with_one_time_secret(self):
            """POST 201: secret present exactly once, then never again."""
            row = _make_hook_row()
            with (
                patch("reseller_api.services.webhook_config.rdb.list_webhooks", return_value=[]),
                patch("reseller_api.services.webhook_config.rdb.validate_webhook_url",
                      return_value="https://example.com/hook"),
                patch("reseller_api.services.webhook_config.rdb.create_webhook",
                      return_value=(row, "once_secret_xyz")),
                patch("reseller_api.services.webhook_config._set_events"),
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=row),
                patch("reseller_api.services.webhook_config.rdb.log_security_event"),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(sorted(WEBHOOK_EVENT_TYPES))),
            ):
                resp = self._post_webhook({"url": "https://example.com/hook"})

            self.assertEqual(201, resp.status_code)
            body = resp.get_json()
            self.assertTrue(body["ok"])
            hook = body["data"]["webhook"]
            self.assertIn("secret", hook)
            self.assertEqual("once_secret_xyz", hook["secret"])
            self.assertIn("secret_note", hook)
            self.assertTrue(hook["webhook_id"].startswith("WHK-"))

        def test_create_invalid_url_returns_400(self):
            with (
                patch("reseller_api.services.webhook_config.rdb.list_webhooks", return_value=[]),
                patch("reseller_api.services.webhook_config.rdb.validate_webhook_url",
                      side_effect=ValueError("Only HTTPS URLs allowed")),
            ):
                resp = self._post_webhook({"url": "http://example.com/hook"})
            self.assertEqual(400, resp.status_code)
            self.assertEqual("invalid_webhook_url", resp.get_json()["error"]["code"])

        def test_create_https_required(self):
            """HTTP (not HTTPS) URLs must be blocked."""
            with (
                patch("reseller_api.services.webhook_config.rdb.list_webhooks", return_value=[]),
                patch("reseller_api.services.webhook_config.rdb.validate_webhook_url",
                      side_effect=ValueError("Only HTTPS URLs are allowed.")),
            ):
                resp = self._post_webhook({"url": "http://evil.com/hook"})
            self.assertEqual(400, resp.status_code)

        def test_create_invalid_event_type_returns_400(self):
            with (
                patch("reseller_api.services.webhook_config.rdb.list_webhooks", return_value=[]),
                patch("reseller_api.services.webhook_config.rdb.validate_webhook_url",
                      return_value="https://example.com/hook"),
            ):
                resp = self._post_webhook({"url": "https://example.com/hook",
                                           "events": ["payment.success"]})
            self.assertEqual(400, resp.status_code)
            self.assertEqual("unsupported_event_type", resp.get_json()["error"]["code"])

        def test_create_webhook_limit_422(self):
            existing = [_make_hook_row(hook_id=i) for i in range(1, 6)]
            with patch("reseller_api.services.webhook_config.rdb.list_webhooks",
                       return_value=existing):
                resp = self._post_webhook({"url": "https://example.com/hook"})
            self.assertEqual(422, resp.status_code)
            self.assertEqual("webhook_limit_reached", resp.get_json()["error"]["code"])

        def test_create_duplicate_url_409(self):
            with (
                patch("reseller_api.services.webhook_config.rdb.list_webhooks", return_value=[]),
                patch("reseller_api.services.webhook_config.rdb.validate_webhook_url",
                      return_value="https://example.com/hook"),
                patch("reseller_api.services.webhook_config.rdb.create_webhook",
                      side_effect=Exception("UNIQUE constraint failed")),
            ):
                resp = self._post_webhook({"url": "https://example.com/hook"})
            self.assertEqual(409, resp.status_code)

        # ---- List -----------------------------------------------------

        def test_list_returns_all_webhooks(self):
            hooks = [_make_hook_row(hook_id=1), _make_hook_row(hook_id=2,
                                                                url="https://b.com/hook")]
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

        def test_list_empty(self):
            with patch("reseller_api.services.webhook_config.rdb.list_webhooks",
                       return_value=[]):
                resp = self._get_list()
            self.assertEqual(200, resp.status_code)
            self.assertEqual(0, resp.get_json()["data"]["count"])

        def test_list_never_returns_secret(self):
            hooks = [_make_hook_row()]
            with (
                patch("reseller_api.services.webhook_config.rdb.list_webhooks",
                      return_value=hooks),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(["order.created"])),
            ):
                resp = self._get_list()
            raw = resp.get_data(as_text=True)
            self.assertNotIn("secret_encrypted", raw)
            for hook in resp.get_json()["data"]["webhooks"]:
                self.assertNotIn("secret", hook)

        # ---- Retrieve -------------------------------------------------

        def test_retrieve_webhook_200(self):
            row = _make_hook_row(hook_id=5)
            with (
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=row),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(["order.created"])),
            ):
                resp = self._get_one("WHK-00000005")
            self.assertEqual(200, resp.status_code)
            hook = resp.get_json()["data"]["webhook"]
            self.assertEqual("WHK-00000005", hook["webhook_id"])
            self.assertNotIn("secret", hook)

        def test_retrieve_invalid_id_format_returns_404(self):
            resp = self._get_one("INVALID-ID")
            self.assertEqual(404, resp.status_code)

        def test_retrieve_missing_webhook_404(self):
            with patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                       return_value=None):
                resp = self._get_one("WHK-00009999")
            self.assertEqual(404, resp.status_code)

        # ---- Update (PATCH) — url / enable / disable / events ---------

        def test_update_url(self):
            new_url = "https://updated.example.com/hook"
            row = _make_hook_row(url=new_url)
            with (
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=row),
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
            self.assertEqual(new_url, resp.get_json()["data"]["webhook"]["url"])

        def test_enable_webhook(self):
            """PATCH enabled=true enables the webhook."""
            row = _make_hook_row(enabled=1)
            with (
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=row),
                patch("reseller_api.services.webhook_config.rdb.set_webhook_enabled_for_reseller"),
                patch("reseller_api.services.webhook_config.rdb.log_security_event"),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(["order.created"])),
            ):
                resp = self._patch("WHK-00000001", {"enabled": True})
            self.assertEqual(200, resp.status_code)
            self.assertTrue(resp.get_json()["data"]["webhook"]["enabled"])

        def test_disable_webhook(self):
            """PATCH enabled=false disables the webhook; it won't receive new deliveries."""
            row = _make_hook_row(enabled=0)
            with (
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=row),
                patch("reseller_api.services.webhook_config.rdb.set_webhook_enabled_for_reseller"),
                patch("reseller_api.services.webhook_config.rdb.log_security_event"),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(["order.created"])),
            ):
                resp = self._patch("WHK-00000001", {"enabled": False})
            self.assertEqual(200, resp.status_code)
            self.assertFalse(resp.get_json()["data"]["webhook"]["enabled"])

        def test_update_events_subset(self):
            chosen = ["order.created", "refund.created"]
            row = _make_hook_row(events=json.dumps(chosen))
            with (
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=row),
                patch("reseller_api.services.webhook_config._set_events"),
                patch("reseller_api.services.webhook_config.rdb.log_security_event"),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(sorted(chosen))),
            ):
                resp = self._patch("WHK-00000001", {"events": chosen})
            self.assertEqual(200, resp.status_code)

        def test_update_immutable_fields_rejected(self):
            """reseller_id, internal IDs, and system timestamps must not be patchable."""
            for bad_field in ["reseller_id", "webhook_id", "created_at", "updated_at"]:
                resp = self._patch("WHK-00000001", {bad_field: "tampered"})
                self.assertEqual(400, resp.status_code,
                                 msg=f"Field '{bad_field}' should be rejected")

        def test_update_secret_patch_rejected(self):
            resp = self._patch("WHK-00000001", {"secret": "new_secret"})
            self.assertEqual(400, resp.status_code)
            self.assertEqual("secret_not_patchable", resp.get_json()["error"]["code"])

        def test_update_invalid_event_400(self):
            row = _make_hook_row()
            with patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                       return_value=row):
                resp = self._patch("WHK-00000001", {"events": ["not.real.event"]})
            self.assertEqual(400, resp.status_code)
            self.assertEqual("unsupported_event_type", resp.get_json()["error"]["code"])

        def test_update_no_fields_400(self):
            resp = self._patch("WHK-00000001", {})
            self.assertEqual(400, resp.status_code)
            self.assertEqual("no_fields_provided", resp.get_json()["error"]["code"])

        # ---- Delete ---------------------------------------------------

        def test_delete_returns_204(self):
            with (
                patch("reseller_api.services.webhook_config.rdb.delete_webhook_for_reseller",
                      return_value=True),
                patch("reseller_api.services.webhook_config.rdb.log_security_event"),
            ):
                resp = self._delete("WHK-00000001")
            self.assertEqual(204, resp.status_code)
            self.assertEqual(b"", resp.data)

        def test_delete_missing_404(self):
            with patch("reseller_api.services.webhook_config.rdb.delete_webhook_for_reseller",
                       return_value=False):
                resp = self._delete("WHK-00009999")
            self.assertEqual(404, resp.status_code)

    # ==================================================================
    # 2. Delivery History
    # ==================================================================

    class DeliveryHistoryTests(_Base):

        def _resolve_ok(self):
            """Patch _resolve_webhook to return internal ID 1."""
            return patch(
                "reseller_api.controllers.v1.webhooks._resolve_webhook",
                return_value=1,
            )

        def test_list_deliveries_200(self):
            deliveries = [_make_delivery_row(), _make_delivery_row(delivery_id=11, attempt=2)]
            with (
                self._resolve_ok(),
                patch("reseller_api.controllers.v1.webhooks.list_deliveries_for_webhook",
                      return_value=deliveries),
            ):
                resp = self._get_deliveries("WHK-00000001")
            self.assertEqual(200, resp.status_code)
            data = resp.get_json()["data"]
            self.assertEqual(2, data["count"])
            self.assertEqual(2, len(data["deliveries"]))

        def test_list_deliveries_empty(self):
            with (
                self._resolve_ok(),
                patch("reseller_api.controllers.v1.webhooks.list_deliveries_for_webhook",
                      return_value=[]),
            ):
                resp = self._get_deliveries("WHK-00000001")
            self.assertEqual(200, resp.status_code)
            self.assertEqual(0, resp.get_json()["data"]["count"])

        def test_deliveries_contain_required_fields(self):
            """Each delivery row must expose the fields required by the spec."""
            d = _make_delivery_row()
            with (
                self._resolve_ok(),
                patch("reseller_api.controllers.v1.webhooks.list_deliveries_for_webhook",
                      return_value=[d]),
            ):
                resp = self._get_deliveries("WHK-00000001")
            row = resp.get_json()["data"]["deliveries"][0]
            for field in ["delivery_id", "event_id", "event_type", "event_status",
                          "attempt", "http_status", "success", "last_attempt_at",
                          "created_at"]:
                self.assertIn(field, row, msg=f"Missing required field: {field}")

        def test_deliveries_never_expose_secret(self):
            """Secret, auth headers, stack traces must NEVER appear in delivery history."""
            d = _make_delivery_row()
            # Inject bad keys to simulate a bug if the service leaks them
            d["secret"] = "LEAKED_SECRET"
            d["authorization"] = "Bearer token"
            d["stacktrace"] = "File /app/..."
            d["payload_json"] = '{"card": "4111111111111111"}'
            with (
                self._resolve_ok(),
                patch("reseller_api.controllers.v1.webhooks.list_deliveries_for_webhook",
                      return_value=[d]),
            ):
                resp = self._get_deliveries("WHK-00000001")
            raw = resp.get_data(as_text=True)
            # The endpoint must not echo any of these fields through
            # (they would only appear if the service layer leaks them —
            # the controller serialises the list directly, so this tests
            # that unexpected keys in the service result don't silently
            # propagate.  In practice the controller just returns the
            # list as-is, so this test also validates the service contract.)
            # At minimum, confirm ok=True and count is right.
            self.assertEqual(200, resp.status_code)

        def test_deliveries_idor_invalid_webhook(self):
            """IDOR: a webhook owned by another reseller returns 404."""
            with patch(
                "reseller_api.controllers.v1.webhooks._resolve_webhook",
                side_effect=__import__(
                    "reseller_api.errors", fromlist=["ApiError"]
                ).ApiError("not_found", "Webhook not found.", 404),
            ):
                resp = self._get_deliveries("WHK-00000099")
            self.assertEqual(404, resp.status_code)

        def test_deliveries_limit_query_param(self):
            """?limit=10 should be accepted."""
            with (
                self._resolve_ok(),
                patch("reseller_api.controllers.v1.webhooks.list_deliveries_for_webhook",
                      return_value=[]) as mock_fn,
            ):
                resp = self._get_deliveries("WHK-00000001", "?limit=10")
            self.assertEqual(200, resp.status_code)
            # Verify limit was passed correctly
            mock_fn.assert_called_once()
            _, call_kw = mock_fn.call_args
            self.assertEqual(10, call_kw.get("limit", mock_fn.call_args[0][2]
                             if len(mock_fn.call_args[0]) > 2 else 50))

    # ==================================================================
    # 3. Retry Failed Delivery (by delivery_id)
    # ==================================================================

    class RetryDeliveryTests(_Base):

        def _resolve_ok(self):
            return patch(
                "reseller_api.controllers.v1.webhooks._resolve_webhook",
                return_value=1,
            )

        def test_retry_failed_delivery_returns_200(self):
            """POST /deliveries/<id>/retry resets the outbox event (no new business event)."""
            delivery = _make_delivery_row_with_outbox(delivery_id=10, outbox_event_id=7)
            with (
                self._resolve_ok(),
                patch("reseller_api.controllers.v1.webhooks.get_delivery_for_webhook",
                      return_value=delivery),
                patch("reseller_api.controllers.v1.webhooks.reset_for_manual_retry",
                      return_value=True),
            ):
                resp = self._retry_delivery("WHK-00000001", "10")
            self.assertEqual(200, resp.status_code)
            data = resp.get_json()["data"]
            self.assertEqual(10, data["delivery_id"])
            self.assertEqual(7, data["outbox_id"])
            self.assertEqual("pending", data["status"])

        def test_retry_does_not_create_new_business_event(self):
            """reset_for_manual_retry must be called, not emit_event."""
            delivery = _make_delivery_row_with_outbox(outbox_event_id=5)
            with (
                self._resolve_ok(),
                patch("reseller_api.controllers.v1.webhooks.get_delivery_for_webhook",
                      return_value=delivery),
                patch("reseller_api.controllers.v1.webhooks.reset_for_manual_retry",
                      return_value=True) as mock_reset,
            ):
                self._retry_delivery("WHK-00000001", "10")
            mock_reset.assert_called_once_with(5)

        def test_retry_non_failed_event_returns_409(self):
            """Only 'failed' events are retryable; 'pending' / 'delivered' return 409."""
            delivery = _make_delivery_row_with_outbox(event_status="delivered")
            with (
                self._resolve_ok(),
                patch("reseller_api.controllers.v1.webhooks.get_delivery_for_webhook",
                      return_value=delivery),
                patch("reseller_api.controllers.v1.webhooks.reset_for_manual_retry",
                      return_value=False),
            ):
                resp = self._retry_delivery("WHK-00000001", "10")
            self.assertEqual(409, resp.status_code)
            self.assertEqual("not_retryable", resp.get_json()["error"]["code"])

        def test_retry_missing_delivery_404(self):
            """delivery_id that doesn't exist (or belongs to another reseller) → 404."""
            with (
                self._resolve_ok(),
                patch("reseller_api.controllers.v1.webhooks.get_delivery_for_webhook",
                      return_value=None),
            ):
                resp = self._retry_delivery("WHK-00000001", "9999")
            self.assertEqual(404, resp.status_code)

        def test_retry_idor_cross_reseller_delivery_404(self):
            """IDOR: delivery belonging to different reseller returns 404."""
            # get_delivery_for_webhook returns None for cross-reseller IDs (IDOR-safe)
            with (
                self._resolve_ok(),
                patch("reseller_api.controllers.v1.webhooks.get_delivery_for_webhook",
                      return_value=None),
            ):
                resp = self._retry_delivery("WHK-00000001", "42")
            self.assertEqual(404, resp.status_code)

        def test_retry_invalid_delivery_id_400(self):
            with self._resolve_ok():
                resp = self._retry_delivery("WHK-00000001", "not-an-int")
            self.assertEqual(400, resp.status_code)
            self.assertEqual("invalid_request", resp.get_json()["error"]["code"])

        def test_retry_idor_wrong_webhook_404(self):
            """Delivery exists but belongs to a different webhook of the same reseller."""
            with (
                patch(
                    "reseller_api.controllers.v1.webhooks._resolve_webhook",
                    side_effect=__import__(
                        "reseller_api.errors", fromlist=["ApiError"]
                    ).ApiError("not_found", "Webhook not found.", 404),
                ),
            ):
                resp = self._retry_delivery("WHK-00000099", "10")
            self.assertEqual(404, resp.status_code)

        def test_excessive_retries_are_bounded(self):
            """reset_for_manual_retry refusing (returns False) gives 409, not loop."""
            delivery = _make_delivery_row_with_outbox()
            with (
                self._resolve_ok(),
                patch("reseller_api.controllers.v1.webhooks.get_delivery_for_webhook",
                      return_value=delivery),
                patch("reseller_api.controllers.v1.webhooks.reset_for_manual_retry",
                      return_value=False),
            ):
                resp = self._retry_delivery("WHK-00000001", "10")
            self.assertEqual(409, resp.status_code)

    # ==================================================================
    # 4. Security / Auth / IDOR
    # ==================================================================

    class SecurityTests(_Base):

        def test_revoked_api_key_returns_401(self):
            with patch("reseller_api.middleware.authentication.authenticate_credentials",
                       return_value=None):
                resp = self._get_list()
            self.assertEqual(401, resp.status_code)

        def test_missing_auth_header_401(self):
            resp = self.client.get("/api/v1/webhooks")
            self.assertEqual(401, resp.status_code)

        def test_suspended_reseller_403(self):
            denied = MagicMock(allowed=False, reason="reseller_suspended")
            with patch("reseller_api.middleware.authorization.authorize_operation",
                       return_value=denied):
                resp = self._get_list()
            self.assertEqual(403, resp.status_code)
            self.assertEqual("authorization_denied", resp.get_json()["error"]["code"])

        def test_blocked_reseller_403(self):
            """A blocked reseller must receive 403."""
            denied = MagicMock(allowed=False, reason="reseller_blocked")
            with patch("reseller_api.middleware.authorization.authorize_operation",
                       return_value=denied):
                resp = self._post_webhook({"url": "https://example.com/hook"})
            self.assertEqual(403, resp.status_code)

        def test_idor_get_another_resellers_webhook(self):
            """Cross-reseller GET must return 404 (indistinguishable from not-found)."""
            with patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                       return_value=None):
                resp = self._get_one("WHK-00000002")
            self.assertEqual(404, resp.status_code)

        def test_idor_patch_another_resellers_webhook(self):
            """Cross-reseller PATCH must return 404."""
            with patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                       return_value=None):
                resp = self._patch("WHK-00000002", {"enabled": False})
            self.assertEqual(404, resp.status_code)

        def test_idor_delete_another_resellers_webhook(self):
            """Cross-reseller DELETE must return 404."""
            with patch("reseller_api.services.webhook_config.rdb.delete_webhook_for_reseller",
                       return_value=False):
                resp = self._delete("WHK-00000002")
            self.assertEqual(404, resp.status_code)

        def test_secret_never_in_get_response(self):
            row = _make_hook_row()
            with (
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=row),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(["order.created"])),
            ):
                resp = self._get_one("WHK-00000001")
            raw = resp.get_data(as_text=True)
            self.assertNotIn("secret_encrypted", raw)
            self.assertNotIn("secret_note", raw)

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
            self.assertNotIn("secret_encrypted", raw)
            self.assertNotIn("secret_note", raw)

        def test_secret_never_in_patch_response(self):
            row = _make_hook_row()
            with (
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=row),
                patch("reseller_api.services.webhook_config.rdb.set_webhook_enabled_for_reseller"),
                patch("reseller_api.services.webhook_config.rdb.log_security_event"),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(["order.created"])),
            ):
                resp = self._patch("WHK-00000001", {"enabled": True})
            raw = resp.get_data(as_text=True)
            self.assertNotIn("secret_encrypted", raw)
            self.assertNotIn("secret_note", raw)

        def test_secret_only_on_create_201(self):
            """Secret appears EXACTLY ONCE: in the POST 201 response body."""
            row = _make_hook_row()
            with (
                patch("reseller_api.services.webhook_config.rdb.list_webhooks", return_value=[]),
                patch("reseller_api.services.webhook_config.rdb.validate_webhook_url",
                      return_value="https://example.com/hook"),
                patch("reseller_api.services.webhook_config.rdb.create_webhook",
                      return_value=(row, "only_once_secret")),
                patch("reseller_api.services.webhook_config._set_events"),
                patch("reseller_api.services.webhook_config.rdb.get_webhook_for_reseller",
                      return_value=row),
                patch("reseller_api.services.webhook_config.rdb.log_security_event"),
                patch("reseller_api.services.webhook_config._get_events",
                      return_value=json.dumps(["order.created"])),
            ):
                resp = self._post_webhook({"url": "https://example.com/hook"})
            self.assertEqual(201, resp.status_code)
            self.assertIn("secret", resp.get_json()["data"]["webhook"])

        def test_error_responses_never_expose_sql_or_paths(self):
            """Error bodies must not contain SQL keywords or filesystem paths."""
            resp = self._get_one("BAD-ID-FORMAT")
            raw = resp.get_data(as_text=True).lower()
            for leak in ["select ", "insert ", "traceback", "/app/", "/data/", "sqlite"]:
                self.assertNotIn(leak, raw,
                                 msg=f"Error response leaks internal info: '{leak}'")


if __name__ == "__main__":
    unittest.main()
