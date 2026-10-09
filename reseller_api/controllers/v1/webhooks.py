"""Webhook configuration controllers — Phase 9 Prompt 2.

Endpoints
---------
  POST   /api/v1/webhooks                 create_webhook_endpoint
  GET    /api/v1/webhooks                 list_webhooks_endpoint
  GET    /api/v1/webhooks/<webhook_id>    get_webhook_endpoint
  PATCH  /api/v1/webhooks/<webhook_id>    update_webhook_endpoint
  DELETE /api/v1/webhooks/<webhook_id>    delete_webhook_endpoint

Security invariants
-------------------
  * reseller_id comes ONLY from the authenticated API context (g.api_context).
  * Webhook IDs supplied by the client are treated as untrusted input and
    validated before any DB query.  A non-existent or cross-reseller ID
    returns 404, indistinguishable from an invalid ID.
  * The webhook signing secret is returned ONCE on creation.  It is absent
    from every other response shape.
"""

from flask import request

from reseller_api.middleware.authorization import (
    get_authenticated_context,
    require_operation,
)
from reseller_api.responses import success_response
from reseller_api.errors import ApiError
from reseller_api.services.authorization import WEBHOOKS_READ, WEBHOOKS_WRITE
import database.reseller_db as rdb
from reseller_api.services.webhook_config import (
    WEBHOOK_EVENT_TYPES,
    WebhookConfigError,
    create_webhook,
    list_webhooks,
    get_webhook,
    update_webhook,
    delete_webhook,
    rotate_webhook_secret,
)
from reseller_api.workers.webhook_worker import reset_for_manual_retry
from reseller_api.services.webhook_delivery import (
    list_deliveries_for_webhook,
    get_delivery_for_webhook,
    get_webhook_secret_by_id,
    _send_http,
)
from reseller_api.services.webhook_config import _internal_id as _parse_whk_id

from reseller_api.services.authorization import WEBHOOKS_WRITE



# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _service_error(exc: WebhookConfigError) -> ApiError:
    return ApiError(exc.code, exc.message, exc.status)


# ---------------------------------------------------------------------------
# POST /api/v1/webhooks
# ---------------------------------------------------------------------------

@require_operation(WEBHOOKS_WRITE)
def create_webhook_endpoint():
    """Register a new webhook destination for the authenticated reseller.

    Accepted JSON body fields
    -------------------------
    url      (string, required)  — HTTPS endpoint, SSRF-validated
    events   (list,   optional)  — event types to subscribe to;
                                   defaults to all supported types
    enabled  (bool,   optional)  — default true

    NOT accepted / NOT trusted from the client
    ------------------------------------------
    reseller_id, secret, webhook_id, created_at, updated_at

    Response (201)
    --------------
    {
      "ok": true,
      "data": {
        "webhook": {
          "webhook_id": "WHK-00000001",
          "url":        "https://example.com/hook",
          "enabled":    true,
          "events":     ["order.created", ...],
          "created_at": "2026-09-29 18:00:00",
          "updated_at": "2026-09-29 18:00:00",
          "secret":     "<plaintext — shown ONCE>",
          "secret_note": "Store this securely ..."
        },
        "supported_events": [...]
      }
    }
    """
    context = get_authenticated_context()

    body = request.get_json(silent=True, force=False)
    if not body or not isinstance(body, dict):
        raise ApiError(
            "invalid_request",
            "A valid JSON request body with Content-Type: application/json is required.",
            400,
        )

    url = body.get("url")
    if not url or not isinstance(url, str):
        raise ApiError("invalid_request", "'url' is required and must be a string.", 400)

    events  = body.get("events")   # None → service applies default
    enabled = body.get("enabled", True)

    if not isinstance(enabled, bool):
        raise ApiError("invalid_request", "'enabled' must be a boolean.", 400)

    try:
        hook = create_webhook(context, url, events=events, enabled=enabled)
    except WebhookConfigError as exc:
        raise _service_error(exc) from None

    return success_response(
        {
            "webhook":          hook,
            "supported_events": sorted(WEBHOOK_EVENT_TYPES),
        },
        status=201,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/webhooks
# ---------------------------------------------------------------------------

@require_operation(WEBHOOKS_READ)
def list_webhooks_endpoint():
    """List all webhooks for the authenticated reseller.

    Secrets are never returned.
    """
    context = get_authenticated_context()
    try:
        hooks = list_webhooks(context)
    except WebhookConfigError as exc:
        raise _service_error(exc) from None
    return success_response({"webhooks": hooks, "count": len(hooks)})


# ---------------------------------------------------------------------------
# GET /api/v1/webhooks/<webhook_id>
# ---------------------------------------------------------------------------

@require_operation(WEBHOOKS_READ)
def get_webhook_endpoint(webhook_id: str):
    """Retrieve a single webhook by public ID.

    IDOR: the DB query is always scoped to the authenticated reseller_id.
    A valid ID belonging to another reseller returns 404.
    Secret is never returned.
    """
    context = get_authenticated_context()
    try:
        hook = get_webhook(context, webhook_id)
    except WebhookConfigError as exc:
        raise _service_error(exc) from None
    return success_response({"webhook": hook})


# ---------------------------------------------------------------------------
# PATCH /api/v1/webhooks/<webhook_id>
# ---------------------------------------------------------------------------

@require_operation(WEBHOOKS_WRITE)
def update_webhook_endpoint(webhook_id: str):
    """Partially update a webhook.

    Patchable fields: url, enabled, events.
    Secret is NOT patchable; trying to patch it returns 400.
    At least one valid field must be provided.
    """
    context = get_authenticated_context()

    body = request.get_json(silent=True, force=False)
    if body is None or not isinstance(body, dict):
        raise ApiError(
            "invalid_request",
            "A valid JSON request body with Content-Type: application/json is required.",
            400,
        )
    # An empty JSON object is a well-formed body: it flows to the service,
    # which reports the specific "no_fields_provided" contract error.

    try:
        hook = update_webhook(context, webhook_id, patch=body)
    except WebhookConfigError as exc:
        raise _service_error(exc) from None

    return success_response({"webhook": hook})


# ---------------------------------------------------------------------------
# DELETE /api/v1/webhooks/<webhook_id>
# ---------------------------------------------------------------------------

@require_operation(WEBHOOKS_WRITE)
def delete_webhook_endpoint(webhook_id: str):
    """Delete a webhook by public ID.

    IDOR: always scoped to the authenticated reseller_id.
    Returns 204 No Content on success.
    """
    context = get_authenticated_context()
    try:
        delete_webhook(context, webhook_id)
    except WebhookConfigError as exc:
        raise _service_error(exc) from None
    # 204 has no body; success_response would add JSON — return raw response.
    from flask import current_app
    resp = current_app.response_class(status=204)
    return resp



# ---------------------------------------------------------------------------
# POST /api/v1/webhooks/<webhook_id>/rotate-secret
# ---------------------------------------------------------------------------

@require_operation(WEBHOOKS_WRITE)
def rotate_secret_endpoint(webhook_id: str):
    """Rotate the signing secret for a webhook.

    Returns the new plaintext secret ONCE in the response body.
    The old secret is invalidated immediately on success.

    Security invariants
    -------------------
    * reseller_id comes only from the authenticated API context.
    * Secret is NOT stored in plaintext; the caller must save it immediately.
    * This endpoint accepts no request body; all inputs come from auth context
      + the URL-path webhook_id.

    Response (200)
    --------------
    {
      "ok": true,
      "data": {
        "webhook_id":  "WHK-00000001",
        "secret":      "<new plaintext secret, shown ONCE>",
        "secret_note": "Store this securely ..."
      }
    }
    """
    context = get_authenticated_context()
    try:
        result = rotate_webhook_secret(context, webhook_id)
    except WebhookConfigError as exc:
        raise _service_error(exc) from None
    return success_response(result)



# ---------------------------------------------------------------------------
# POST /api/v1/webhooks/events/<outbox_id>/retry
# ---------------------------------------------------------------------------

@require_operation(WEBHOOKS_WRITE)
def retry_event_endpoint(outbox_id: str):
    """Manually retry a dead-lettered webhook event.

    Resets the existing outbox row (status='failed' -> 'pending') so the
    delivery worker re-delivers the original event payload on the next poll.

    IMPORTANT: This does NOT create a new business event.  It only resets
    delivery state for an event that has already been committed to the outbox.
    The event_id, order_id, and payload are unchanged.

    Security
    --------
    * reseller_id comes only from the authenticated API context.
    * The outbox_id from the URL is an integer; non-integer values return 400.
    * Cross-reseller access: the service layer verifies the outbox row belongs
      to the authenticated reseller before resetting it.

    Response (200)
    --------------
    { "ok": true, "data": { "outbox_id": 42, "status": "pending" } }

    Response (404) — event not in 'failed' state or not found
    Response (400) — outbox_id is not a valid integer
    """
    context = get_authenticated_context()
    try:
        oid = int(outbox_id)
    except (ValueError, TypeError):
        raise ApiError("invalid_request", "outbox_id must be an integer.", 400)

    # Verify this outbox row belongs to the authenticated reseller
    from database.db import get_cursor
    cur = get_cursor()
    cur.execute(
        "SELECT id FROM webhook_outbox WHERE id=? AND reseller_id=?",
        (oid, context.reseller_id),
    )
    if not cur.fetchone():
        raise ApiError("not_found", f"Webhook event {outbox_id!r} not found.", 404)

    ok = reset_for_manual_retry(oid)
    if not ok:
        raise ApiError(
            "not_retryable",
            f"Event {outbox_id} is not in a retryable state (must be 'failed').",
            409,
        )
    return success_response({"outbox_id": oid, "status": "pending"})



# ---------------------------------------------------------------------------
# Private helper — resolve & ownership-check a public webhook ID
# ---------------------------------------------------------------------------

def _resolve_webhook(context, public_webhook_id: str) -> int:
    """Parse WHK-XXXXXXXX → internal int and verify ownership.

    Raises ApiError(404) if the ID is malformed, does not exist, or belongs
    to a different reseller.  The 404 is intentionally indistinguishable from
    'not found' to prevent IDOR enumeration.
    """
    try:
        internal_id = _parse_whk_id(public_webhook_id)
    except Exception:
        raise ApiError("not_found", "Webhook not found.", 404)

    from database.db import get_cursor
    cur = get_cursor()
    cur.execute(
        "SELECT id FROM reseller_webhooks WHERE id=? AND reseller_id=?",
        (internal_id, context.reseller_id),
    )
    if not cur.fetchone():
        raise ApiError("not_found", "Webhook not found.", 404)
    return internal_id


# ---------------------------------------------------------------------------
# GET /api/v1/webhooks/<webhook_id>/deliveries
# ---------------------------------------------------------------------------

@require_operation(WEBHOOKS_READ)
def list_deliveries_endpoint(webhook_id: str):
    """Return delivery history for a single webhook (newest first, max 100).

    IDOR: webhook_id is scoped to the authenticated reseller_id.
    A valid ID owned by another reseller returns 404.

    Safe fields exposed per delivery
    ---------------------------------
    delivery_id, event_id, event_type, event_status, attempt, http_status,
    success, error_category, last_attempt_at, next_retry_at, created_at.

    Fields NEVER exposed
    --------------------
    webhook secret, Authorization headers, stack traces, payload_json,
    internal DB paths, raw SQL.

    Response (200)
    --------------
    { "ok": true, "data": { "deliveries": [...], "count": N } }
    """
    context = get_authenticated_context()
    internal_id = _resolve_webhook(context, webhook_id)

    try:
        limit = min(max(1, int(request.args.get("limit", 50))), 100)
    except (ValueError, TypeError):
        limit = 50

    deliveries = list_deliveries_for_webhook(context.reseller_id, internal_id, limit=limit)
    return success_response({"deliveries": deliveries, "count": len(deliveries)})


# ---------------------------------------------------------------------------
# POST /api/v1/webhooks/<webhook_id>/deliveries/<delivery_id>/retry
# ---------------------------------------------------------------------------

@require_operation(WEBHOOKS_WRITE)
def retry_delivery_endpoint(webhook_id: str, delivery_id: str):
    """Retry a failed delivery by its delivery_id.

    Resets the underlying outbox event (failed → pending) so the delivery
    worker re-delivers the ORIGINAL payload on the next poll.

    Security invariants
    --------------------
    * reseller_id comes ONLY from the authenticated API context.
    * Both webhook_id and delivery_id are IDOR-checked before any state change.
    * Only events in status='failed' can be retried (409 otherwise).
    * The original business event (event_id, payload, idempotency_key) is
      NEVER duplicated.  No new outbox row is created.

    Abuse protection
    ----------------
    * Requires WEBHOOKS_WRITE permission (rate-limited by the existing quota).
    * Delivery must belong to the reseller's webhook (double ownership check).
    * Non-failed events return 409 (idempotent — safe to retry on client error).

    Response (200)
    --------------
    { "ok": true, "data": { "delivery_id": 42, "outbox_id": 7, "status": "pending" } }

    Response (404) — delivery not found or IDOR
    Response (409) — event not in retryable state
    Response (400) — delivery_id not an integer
    """
    context = get_authenticated_context()
    internal_webhook_id = _resolve_webhook(context, webhook_id)

    try:
        did = int(delivery_id)
    except (ValueError, TypeError):
        raise ApiError("invalid_request", "delivery_id must be a positive integer.", 400)

    # IDOR-safe: verify delivery belongs to this reseller's specific webhook.
    delivery = get_delivery_for_webhook(context.reseller_id, internal_webhook_id, did)
    if delivery is None:
        raise ApiError("not_found", f"Delivery {delivery_id!r} not found.", 404)

    outbox_id = delivery["outbox_event_id"]
    ok = reset_for_manual_retry(outbox_id)
    if not ok:
        raise ApiError(
            "not_retryable",
            "This event is not in a retryable state. Only failed events can be retried.",
            409,
        )
    return success_response({
        "delivery_id": did,
        "outbox_id":   outbox_id,
        "status":      "pending",
    })


__all__ = [
    "create_webhook_endpoint",
    "list_webhooks_endpoint",
    "get_webhook_endpoint",
    "update_webhook_endpoint",
    "delete_webhook_endpoint",
    "rotate_secret_endpoint",
    "retry_event_endpoint",
    "list_deliveries_endpoint",
    "retry_delivery_endpoint",
]


# ---------------------------------------------------------------------------
# GET /api/v1/webhooks/analytics — Phase 14 P6 (own webhook activity)
# ---------------------------------------------------------------------------

@require_operation(WEBHOOKS_READ)
def webhook_analytics_endpoint():
    """Own webhook/delivery analytics from the outbox + delivery log.
    Counts and rates only — never secrets, URLs of others, or payloads."""
    context = get_authenticated_context()
    return success_response({
        "webhooks": rdb.get_reseller_webhook_analytics(context.reseller_id),
    })


# ---------------------------------------------------------------------------
# POST /api/v1/webhooks/<webhook_id>/test — Phase 13 P7 (sandbox/testing)
# ---------------------------------------------------------------------------

@require_operation(WEBHOOKS_WRITE)
def test_webhook_endpoint(webhook_id: str):
    """Send a signed synthetic `test.ping` event to one of YOUR webhooks.

    Purpose-built for integration testing (sandbox or production): the
    developer verifies their signature verification and handler end to end
    without placing a real order.  The ping:

      * uses the webhook's own registered URL (SSRF-validated at creation;
        the client cannot aim the ping at an arbitrary host)
      * is signed with the webhook's real secret, exactly like a business
        event (same headers, same HMAC scheme)
      * is synchronous — the delivery outcome is the response payload
      * creates no order, stock, wallet, or outbox state
      * is audited as a security event (no secret material)

    Ownership: scoped to the authenticated reseller; a foreign or unknown
    webhook id returns the uniform 404.
    """
    import uuid
    from flask import current_app
    from reseller_api.services import security_logger as sec_log
    from utils.timezone import now_text

    context = get_authenticated_context()
    try:
        hook = get_webhook(context, webhook_id)      # 404 on foreign/unknown
    except WebhookConfigError as exc:
        raise _service_error(exc) from None
    if not hook.get("enabled", True):
        raise ApiError("webhook_disabled",
                       "This webhook is disabled. Enable it before testing.",
                       422)

    internal_id = _parse_whk_id(webhook_id)
    secret = get_webhook_secret_by_id(internal_id, context.reseller_id)
    if not secret:
        raise _service_error(
            WebhookConfigError("webhook_not_found", "Webhook not found.", 404))

    ping_id = f"ping-{uuid.uuid4().hex[:12]}"
    payload = {
        "event": "test.ping",
        "event_type": "test.ping",
        "event_id": ping_id,
        "idempotency_key": ping_id,
        "webhook_id": str(hook["webhook_id"]),
        "environment": current_app.config["API_ENVIRONMENT"],
        "timestamp": now_text(),
        "message": "Webhook connectivity test from the OGGY reseller API.",
    }
    result = _send_http(str(hook["url"]), secret, payload, "test.ping",
                        ping_id)
    try:
        sec_log.emit(
            "webhook_test_ping",
            reseller_id=context.reseller_id,
            reseller_public_id=context.reseller_public_id,
            description=(f"ping to {hook['webhook_id']} -> "
                         f"status={result.get('http_status')} "
                         f"success={result.get('success')}"),
        )
    except Exception:
        pass

    return success_response({
        "test": {
            "webhook_id": str(hook["webhook_id"]),
            "event_type": "test.ping",
            "delivered": bool(result.get("success")),
            "http_status": result.get("http_status"),
            "error_category": result.get("error_category"),
            "elapsed_ms": result.get("elapsed_ms"),
        }
    })
