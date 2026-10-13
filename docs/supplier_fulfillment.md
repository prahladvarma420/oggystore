# Supplier placement and reconciliation — Prompt 16.7

## Provider contract

Owner-only product configuration uses the existing encrypted, write-only JSON
field. `supplier_api` requires `endpoint` (public HTTPS), `api_key` and
`supplier_product_id`. Optional `idempotency_prefix`, HMAC secret, timeouts,
retry count and dispatch budget follow `fulfillment_generic_api.md` validation.
No credentials, configuration or internal supplier reference enter order API
responses, webhook payloads or logs. Base client interface is unchanged.

Placement is a POST to the configured endpoint, with Bearer authorization and
the optional existing timestamp/HMAC signature over the exact JSON body:

```json
{"order_id":"ORD-00000042","product_id":"SUPPLIER-SKU","quantity":2}
```

`Idempotency-Key: reseller-order:42` (or configured prefix) MUST be honored by
the provider: repeated placement for this key must return the same logical
order/reference without charging or fulfilling again. A valid placement
response is `{"status":"pending","reference":"SUP-42"}`. `processing` or
`fulfilled` with a reference also returns pending internally and is completed
out of band. The reference must be 1–200 ASCII alphanumeric/`_.:-` characters,
not a credential. Missing/malformed placement responses fail safely through the
existing post-debit failure engine. Terminal HTTP errors use established error
categories; transient errors have at most three in-call retries within the
1–10-second dispatch budget (default 5 seconds).

A read-only status poll uses **the same endpoint**, authorization, HTTPS/SSRF
policy and optional signature:

```json
{"order_id":"ORD-00000042","product_id":"SUPPLIER-SKU","quantity":2,"action":"status","reference":"SUP-42"}
```

After a lost reference, the `reference` field is omitted: the provider MUST
support lookup by `order_id`. Status polls deliberately omit the placement
Idempotency-Key so a cached placement/pending response is not replayed forever.
The `action: status` operation must never create or charge a new order.

Accepted poll bodies:

- `{"status":"pending","reference":"SUP-42"}` (or `processing`).
- `{"status":"fulfilled","items":["CODE-1","CODE-2"]}` (or `completed`).
- `{"status":"failed"}` (or `cancelled`/`terminal_error`).
- HTTP 404 or `{"status":"not_found"}` means lookup found no order; the worker
  may place using the **same original placement key**, never a new key.

Success requires exactly `quantity` nonempty string items, or the existing
single-quantity `delivery` format. Total content is at most 10,000 characters;
HTML-like text and Unicode are retained, items are joined with the existing
blank-line separator. Malformed/ambiguous polls are not completion: retry with
backoff until a valid outcome or maximum age. Generic external providers with
pending or delivery-write-failed outcomes must implement this same read-only
lookup contract, even if they have no separate supplier reference.

## Durable correlation, completion and failure

Only one schema column is added: nullable `orders.fulfillment_ref TEXT`, via
`_safe_add_column` inside `ensure_order_metadata_columns`. It stores encrypted
versioned metadata, **never delivery content**. Before the existing order/debit
transaction commits, external/supplier orders store an encrypted public-order-ID
correlation. After placement it is compare-and-set upgraded to the encrypted
supplier reference. This covers a pre-dispatch crash, dispatch saturation,
response loss and reference persistence failure. A conflicting supplier
reference cannot overwrite an existing one. Eligible legacy 16.6 orders without
a reference receive a bounded order-ID correlation during reconciliation.

The worker only polls reseller-source, non-terminal orders with a reference and
empty stored delivery. Delivered/customer/manual/code-stock orders are not
polled. SQL uses a primary-key cursor, status/delivery predicates, LIMIT and
indexed settings-key joins for due times. It does not load all orders and
filter them in Python. No `(source, fulfillment_ref)` index is needed at the
current modest scale: batches are bounded and traversal uses the primary key.
This is not a guarantee of constant scan cost on an arbitrarily large sparse
table; monitor batch latency before scaling. Do not add an unapproved index.

Each run uses a unique ownership token and separate `fulfillment-job:N` claim.
A stale claim expires after 120 seconds. Local terminal writes also acquire the
existing `admin-order-lifecycle:N` claim, shared with owner actions; HTTP takes
place outside database transactions and outside that local-action claim.
Completion reuses the 16.4 helper: encryption, safe delivery write, completed
status and strict `delivery.completed` outbox emission in one atomic block.
A persistence/outbox failure rolls back and remains recoverable. Repeat success
never overwrites delivery or emits a second completion event.

Confirmed failure or age >= `FULFILLMENT_MAX_AGE_SECONDS` (default 24 hours)
uses `handle_post_debit_failure`, existing outstanding-refund caps/ledger
references, stock guards, refund records and failure events. A durable failure
intent in the **existing settings table** is written before that crash-resumable
engine. If a process dies after Failed status but before refund/stock completion,
a later worker run repairs only its own automatic failure intent. An admin
terminal order without that intent is skipped. Repair attempts respect backoff
and take at most half a normal batch (minimum one) to preserve ordinary progress.

Retry due/attempt/cursor and notification markers also use existing settings;
there are no new job/retry tables. Poll transient failures use durable jittered
exponential backoff (15 seconds base, 3600 cap); no tight poll retries. Persisted
UTC creation timestamps are compared with the aware shop clock. Maximum age
can override an ordinary poll backoff. Clock-skew and PostgreSQL UTC-session
configuration must be checked in staging.

Failures send a best-effort notification through the shared 16.2 formatter,
using reason `order failed, refunded`, when a Telegram account and BOT_TOKEN
are available. No credentials/delivery are included. The bounded send runs
outside transactions after successful failure/refund processing. A notification
marker is committed before sending: notification is **at-most-once best effort**
and may be lost on a process crash; financial recovery is not lost.

## Operations and verification

Deploy a separate `fulfillment-worker`, not a modification of `webhook-worker`;
see `RAILWAY_RESELLER_API.md` for both commands and required shared database/key.
Changing a product's fulfillment configuration/type while orders are pending
is not a supported provider migration: keep the original provider contract
available until those orders settle. No schema snapshot of credentials is added.

The 16.5 legacy encrypted-empty delivery cleanup remains manual/opt-in; this
worker never automatically decrypts and rewrites legacy delivery history.

Run isolated tests:

```bash
python -m unittest tests.test_phase16_supplier_fulfillment -q
python -m unittest tests.test_phase16_fulfillment_wiring -q
```

New tests use real HTTPS placement/polling with a generated test CA and transport
boundary redirection only; production has no loopback exception or insecure TLS.
They exercise real order creation, wallet/refund services, encrypted storage,
completion/outbox rollback, concurrency, crash resume, stale claims, backoff,
maximum age, notification failure and authenticated v1 delivery. Run Phase 12,
Phase 15, Phase 16 and all existing regressions as well. Actual provider behavior
and PostgreSQL multi-process deployment still require isolated staging checks.
