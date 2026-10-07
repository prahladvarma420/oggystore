"""Webhook configuration service — Phase 9 Prompt 2.

Service layer for reseller webhook CRUD: create, list, retrieve, update,
delete.  All operations are IDOR-safe by always scoping DB queries to the
authenticated reseller_id from the API context.

Secret handling
---------------
  CREATE  — secret generated, stored encrypted, returned ONCE in plaintext.
  GET/LIST — secret is NEVER returned.
  PATCH   — url, enabled, events can be updated; secret is NOT patchable.
             To rotate a secret the reseller must delete and re-create.

Public webhook ID format: WHK-{id:08d}  (mirrors ORD-{id:08d} convention).
"""

import json
import re

import database.reseller_db as rdb
from reseller_api.context import ApiRequestContext


# ---------------------------------------------------------------------------
# Supported event types
# Only types that the delivery layer will actually fire are listed here.
# Unknown event names are rejected at write time so the API surface stays
# stable when new event types are added in later prompts.
# ---------------------------------------------------------------------------

WEBHOOK_EVENT_TYPES = frozenset({
    "order.created",
    "delivery.pending",
    "order.failed",
    "order.cancelled",
    "delivery.completed",
    "delivery.failed",
    "refund.created",
    # Phase 11 P6/P8 — master product sync events (emitted via the outbox).
    # Without these, a reseller using an explicit events list could never
    # subscribe to product sync notifications.
    "product.updated",
    "product.disabled",
    "product.enabled",
    "product.deleted",
    "stock.updated",
})

_PUBLIC_ID_RE = re.compile(r"^WHK-(\d{8})$")
MAX_WEBHOOKS = 5


# ---------------------------------------------------------------------------
# Custom errors
# ---------------------------------------------------------------------------

class WebhookConfigError(ValueError):
    """Raised for business-rule violations."""
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(code)
        self.code = str(code)
        self.message = str(message)
        self.status = int(status)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _public_id(internal_id: int) -> str:
    return f"WHK-{int(internal_id):08d}"


def _internal_id(public_id: str) -> int:
    """Parse WHK-XXXXXXXX -> int, or raise WebhookConfigError."""
    m = _PUBLIC_ID_RE.match(str(public_id or ""))
    if not m:
        raise WebhookConfigError(
            "invalid_webhook_id",
            "The webhook ID is invalid.",
            404,
        )
    return int(m.group(1))


def _parse_events(raw) -> list:
    """Validate and normalise the events list from client input.

    Accepts a list of strings.  Every entry must be in WEBHOOK_EVENT_TYPES.
    An empty list means the reseller explicitly wants no events (effectively
    disabled at the event level); that is allowed.
    """
    if not isinstance(raw, list):
        raise WebhookConfigError(
            "invalid_events",
            "'events' must be a list of event type strings.",
            400,
        )
    cleaned = []
    for item in raw:
        if not isinstance(item, str):
            raise WebhookConfigError(
                "invalid_events",
                "Each event type must be a string.",
                400,
            )
        item = item.strip().lower()
        if item not in WEBHOOK_EVENT_TYPES:
            raise WebhookConfigError(
                "unsupported_event_type",
                f"Event type '{item}' is not supported. "
                f"Supported types: {sorted(WEBHOOK_EVENT_TYPES)}",
                400,
            )
        if item not in cleaned:
            cleaned.append(item)
    return cleaned


def _format_hook(row: dict, secret: str = None) -> dict:
    """Serialise a reseller_webhooks DB row to the public API shape.

    The signing secret is included ONLY when explicitly provided (i.e. on
    creation).  It is never returned by GET or PATCH.
    """
    events_raw = row.get("events")
    try:
        events = json.loads(events_raw) if events_raw else list(WEBHOOK_EVENT_TYPES)
    except Exception:
        events = list(WEBHOOK_EVENT_TYPES)

    out = {
        "webhook_id": _public_id(row["id"]),
        "url":        row["url"],
        "enabled":    bool(row["enabled"]),
        "events":     sorted(events),
        "created_at": row["created"],
        "updated_at": row["updated"],
    }
    if secret is not None:
        # One-time plaintext exposure on creation only.
        out["secret"] = secret
        out["secret_note"] = (
            "Store this secret securely. It will never be shown again. "
            "Delete and recreate the webhook to rotate it."
        )
    return out


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------

def create_webhook(
    context: ApiRequestContext,
    url: str,
    events=None,
    enabled: bool = True,
) -> dict:
    """Create a new webhook for the authenticated reseller.

    Returns the formatted hook including the one-time plaintext secret.
    Raises WebhookConfigError on business-rule violations.
    """
    # Enforce per-reseller limit (mirrors Telegram UI limit).
    existing = rdb.list_webhooks(context.reseller_id)
    if len(existing) >= MAX_WEBHOOKS:
        raise WebhookConfigError(
            "webhook_limit_reached",
            f"Maximum of {MAX_WEBHOOKS} webhooks per reseller.",
            422,
        )

    # Validate URL (SSRF-safe; raises ValueError on failure).
    try:
        normalized_url = rdb.validate_webhook_url(url)
    except (ValueError, TypeError) as exc:
        raise WebhookConfigError("invalid_webhook_url", str(exc), 400) from None

    # Validate and parse events list.
    if events is None:
        # Default: subscribe to all supported events.
        event_list = sorted(WEBHOOK_EVENT_TYPES)
    else:
        event_list = _parse_events(events)

    events_json = json.dumps(sorted(event_list))

    # rdb.create_webhook() generates secret, stores encrypted, returns plaintext.
    try:
        hook_row, plaintext_secret = rdb.create_webhook(
            context.reseller_id, normalized_url, enabled=bool(enabled)
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "unique" in msg:
            raise WebhookConfigError(
                "webhook_url_conflict",
                "A webhook with this URL is already registered.",
                409,
            ) from None
        raise WebhookConfigError(
            "webhook_create_failed",
            "The webhook could not be created.",
            500,
        ) from None

    # Persist events column (separate update; rdb.create_webhook doesn't take events).
    _set_events(context.reseller_id, hook_row["id"], events_json)

    # Re-fetch to get the events-updated row.
    hook_row = rdb.get_webhook_for_reseller(context.reseller_id, hook_row["id"])
    if hook_row is None:
        raise WebhookConfigError("webhook_create_failed", "Webhook not found after creation.", 500)
    # Inject events into the row dict for formatting (not a real column yet, we patch here).
    hook_row["events"] = events_json

    # Log security event.
    try:
        rdb.log_security_event(
            context.reseller_id,
            "webhook_created",
            description=f"webhook {hook_row['id']} created via API key {context.api_key_public_id}",
        )
    except Exception:
        pass

    return _format_hook(hook_row, secret=plaintext_secret)


def list_webhooks(context: ApiRequestContext) -> list:
    """Return all webhooks for the authenticated reseller (no secrets)."""
    rows = rdb.list_webhooks(context.reseller_id)
    result = []
    for row in rows:
        events_json = _get_events(context.reseller_id, row["id"])
        row["events"] = events_json
        result.append(_format_hook(row))
    return result


def get_webhook(
    context: ApiRequestContext,
    public_webhook_id: str,
) -> dict:
    """Return a single webhook by public ID (no secret)."""
    internal_id = _internal_id(public_webhook_id)
    row = rdb.get_webhook_for_reseller(context.reseller_id, internal_id)
    if row is None:
        raise WebhookConfigError("webhook_not_found", "Webhook not found.", 404)
    events_json = _get_events(context.reseller_id, internal_id)
    row["events"] = events_json
    return _format_hook(row)


def update_webhook(
    context: ApiRequestContext,
    public_webhook_id: str,
    patch: dict,
) -> dict:
    """Partially update a webhook.  Allowed fields: url, enabled, events.

    Secret cannot be updated; unknown fields are rejected.
    """
    # Reject any attempt to patch the secret.
    if "secret" in patch:
        raise WebhookConfigError(
            "secret_not_patchable",
            "The webhook secret cannot be updated. Delete and recreate the webhook to rotate it.",
            400,
        )

    allowed = {"url", "enabled", "events"}
    unknown = set(patch.keys()) - allowed
    if unknown:
        raise WebhookConfigError(
            "unknown_fields",
            f"Unknown field(s): {sorted(unknown)}. Allowed: url, enabled, events.",
            400,
        )

    internal_id = _internal_id(public_webhook_id)

    # Client error first: an empty patch is a 400 regardless of whether the
    # webhook exists (validation errors precede resource lookup).
    if not patch:
        raise WebhookConfigError(
            "no_fields_provided",
            "No updatable fields were provided.",
            400,
        )

    row = rdb.get_webhook_for_reseller(context.reseller_id, internal_id)
    if row is None:
        raise WebhookConfigError("webhook_not_found", "Webhook not found.", 404)

    changed = False

    if "url" in patch:
        try:
            normalized = rdb.validate_webhook_url(patch["url"])
        except (ValueError, TypeError) as exc:
            raise WebhookConfigError("invalid_webhook_url", str(exc), 400) from None
        try:
            ok = rdb.update_webhook_url(context.reseller_id, internal_id, normalized)
        except Exception as exc:
            msg = str(exc).lower()
            if "unique" in msg:
                raise WebhookConfigError(
                    "webhook_url_conflict",
                    "A webhook with this URL is already registered.",
                    409,
                ) from None
            raise WebhookConfigError("webhook_update_failed", "Update failed.", 500) from None
        if not ok:
            raise WebhookConfigError("webhook_not_found", "Webhook not found.", 404)
        changed = True

    if "enabled" in patch:
        val = patch["enabled"]
        if not isinstance(val, bool):
            raise WebhookConfigError(
                "invalid_enabled",
                "'enabled' must be a boolean.",
                400,
            )
        rdb.set_webhook_enabled_for_reseller(context.reseller_id, internal_id, val)
        changed = True

    if "events" in patch:
        event_list = _parse_events(patch["events"])
        _set_events(context.reseller_id, internal_id, json.dumps(sorted(event_list)))
        changed = True

    if not changed:
        raise WebhookConfigError(
            "no_fields_provided",
            "No updatable fields were provided.",
            400,
        )

    # Log.
    try:
        rdb.log_security_event(
            context.reseller_id,
            "webhook_updated",
            description=f"webhook {internal_id} updated via API key {context.api_key_public_id}",
        )
    except Exception:
        pass

    return get_webhook(context, public_webhook_id)


def delete_webhook(
    context: ApiRequestContext,
    public_webhook_id: str,
) -> None:
    """Delete a webhook.  Returns None on success."""
    internal_id = _internal_id(public_webhook_id)
    ok = rdb.delete_webhook_for_reseller(context.reseller_id, internal_id)
    if not ok:
        raise WebhookConfigError("webhook_not_found", "Webhook not found.", 404)

    # Log.
    try:
        rdb.log_security_event(
            context.reseller_id,
            "webhook_removed",
            description=f"webhook {internal_id} deleted via API key {context.api_key_public_id}",
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Events column helpers (additive column on reseller_webhooks)
# ---------------------------------------------------------------------------

def _set_events(reseller_id: int, webhook_id: int, events_json: str):
    """Write the events JSON to the reseller_webhooks row (IDOR-scoped)."""
    from database.db import get_conn, get_cursor
    conn = get_conn()
    cur  = get_cursor()
    cur.execute(
        "UPDATE reseller_webhooks SET events=? WHERE id=? AND reseller_id=?",
        (events_json, int(webhook_id), int(reseller_id)),
    )
    conn.commit()


def _get_events(reseller_id: int, webhook_id: int):
    """Read the raw events JSON for one webhook row."""
    from database.db import get_cursor
    cur = get_cursor()
    cur.execute(
        "SELECT events FROM reseller_webhooks WHERE id=? AND reseller_id=?",
        (int(webhook_id), int(reseller_id)),
    )
    row = cur.fetchone()
    return row[0] if row else None



def rotate_webhook_secret(context, webhook_id: str) -> dict:
    """Rotate the signing secret for a webhook (returns new plaintext secret ONCE).

    Security invariants
    -------------------
    * reseller_id comes only from the authenticated API context.
    * New secret is 256-bit random via secrets.token_urlsafe(32).
    * Plaintext is never stored or logged.
    * Old secret is invalidated immediately on commit.
    """
    internal_id = _internal_id(webhook_id)
    import database.reseller_db as rdb2
    new_secret = rdb2.rotate_webhook_secret(context.reseller_id, internal_id)
    if new_secret is None:
        raise WebhookConfigError(
            "not_found",
            f"Webhook {webhook_id!r} not found.",
            404,
        )
    return {
        "webhook_id":  webhook_id,
        "secret":      new_secret,
        "secret_note": (
            "Your new signing secret. Store it securely — it will not be shown again. "
            "Update your receiver verification logic before retrying pending deliveries."
        ),
    }


__all__ = [
    "WEBHOOK_EVENT_TYPES",
    "MAX_WEBHOOKS",
    "WebhookConfigError",
    "create_webhook",
    "list_webhooks",
    "get_webhook",
    "update_webhook",
    "delete_webhook",
    "rotate_webhook_secret",
]
