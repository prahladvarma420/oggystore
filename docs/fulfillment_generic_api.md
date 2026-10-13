# Generic API fulfillment — Prompt 16.6

## Scope and recovery boundary

External generic fulfillment is wired into NEW reseller orders only, after
wallet/order/outbox commit. Code-stock and manual behavior are retained.
Idempotent HTTP 200 replay never redispatches and does not gain a fulfillment
object; HTTP 201 adds only `fulfillment: {type, status}` to the existing fields.

`GenericApiClient.fulfill(order_id, product_config, quantity)` retains the
existing ExternalFulfillmentClient interface. The client classifies each
result as fulfilled, pending, retryable_error, or terminal_error. Only delivery
content is returned internally; no configuration or arbitrary supplier error
is returned or logged. Business handling maps exhausted transient failures to
terminal_error after invoking the existing refund engine.

Prompt 16.7 adds `SupplierApiClient`, an encrypted order/reference correlation,
and a dedicated reconciliation worker. Generic pending/write-failed outcomes
also use the status lookup contract described in `supplier_fulfillment.md`.
Providers must support lookup by the public order ID and honor stable placement
idempotency; a late response is never allowed to write delivery from its network
thread. The worker must be deployed separately for automatic recovery.

## Owner configuration (write-only)

Example, with placeholders rather than real credentials:

```json
{
  "endpoint": "https://supplier.example/fulfill",
  "api_key": "YOUR_SUPPLIER_CREDENTIAL",
  "hmac_secret": "YOUR_OPTIONAL_SIGNING_SECRET",
  "connect_timeout": 2,
  "read_timeout": 2,
  "retries": 2,
  "dispatch_budget": 5,
  "supplier_product_id": "OPTIONAL_SUPPLIER_PRODUCT_ID",
  "idempotency_prefix": "reseller-order"
}
```

- HTTPS only; credentials in URL, query strings, fragments, localhost/private
  literals and private/non-global DNS answers are rejected.
- Separate connect/read timeouts must each be 1–60 seconds. Legacy `timeout`
  is accepted as a fallback for both. Effective timeouts are capped to the
  remaining dispatch budget; fewer attempts may be made than configured.
- `retries` is an integer 0–3 (additional retries; maximum four HTTP attempts).
- `dispatch_budget` is 1–10 seconds, default 5. If too little time remains to
  attempt connect+read safely, return pending instead of starting another call.
- Credentials are strings without control characters; bearer keys must be
  ASCII. Prefix is at most 128 ASCII alphanumeric/`_.:-` characters.
- Unknown JSON fields, invalid shapes/limits and invalid endpoints are refused
  before product writes. URL/DNS validation runs at save and immediately before
  every connection/retry. Existing plaintext legacy JSON is readable internally;
  new owner saves require encryption configuration and encrypt the whole JSON.
- The form never pre-fills or fetches credentials. Blank configuration preserves
  existing external/supplier settings; replacement JSON replaces the entire
  configuration. Selecting code_stock/manual clears that configuration.
- Only the owner can change fulfillment fields. Existing non-owner general
  product editing authorization is unchanged. Product list/get serialization
  omits raw configuration for all roles, returning only a configured boolean.
- Do not edit provider configuration while orders are unresolved; the 16.6
  schema has no per-order configuration snapshot/reference column.

## Request contract and idempotency

POST JSON, UTF-8:

```json
{"order_id":"ORD-00000042","quantity":2,"product_id":"SUPPLIER-PRODUCT"}
```

`product_id` is included only when supplier_product_id is configured.
`order_id` is the public master order ID. `quantity` is a positive integer
matching the charged/reserved order quantity. The supplier MUST implement
idempotent placement AND recovery/status lookup by this order ID or by the
stable Idempotency-Key. Retrying a request with that key must not buy or charge
again, and future reconciliation must be able to retrieve the original result.
Do not enable an endpoint that lacks this contract.

Headers:

- Content-Type: application/json
- Authorization: Bearer configured api_key (if present)
- Idempotency-Key: `{idempotency_prefix or reseller-order}:{internal order ID}`
- User-Agent: OggyStore-Fulfillment/1.0
- Host: original validated supplier host/port (not the pinned IP)

Optional HMAC:

- X-Fulfillment-Timestamp: epoch seconds
- X-Fulfillment-Signature: `sha256=` + hex HMAC-SHA256
- Signed bytes: `timestamp + b'.' + exact UTF-8 request body`
- Signing key: UTF-8 hmac_secret. Supplier should validate freshness and compare
  signatures in constant time. The idempotency key remains stable across retries.

## Response contract and quantity

Immediate success (HTTP 2xx other than 202):

```json
{"status":"fulfilled","items":["CODE-ONE","CODE-TWO"]}
```

`items` must contain exactly quantity non-empty strings. Total joined delivery
is 1–10,000 characters, joined by two newlines for the existing real delivery
endpoint. For quantity=1 only, `{delivery: "CODE"}` is also accepted. A missing
status is allowed only for a valid delivery/items success body. HTML/Unicode
are treated as text and preserved; no plaintext is placed in creation JSON,
logs or webhook payloads. Echoed configured credentials are rejected as delivery.

Accepted asynchronous response:

```json
{"status":"pending"}
```

HTTP 202 with a JSON object and no explicit status is also pending. An opaque
reference may be returned by a provider, but it is NOT persisted in 16.6; the
required stable order/idempotency lookup contract is the recovery mechanism.
Do not rely on an opaque-reference-only provider until 16.7 implements storage.

HTTP 200 malformed JSON/content/quantity or oversized (>65,536-byte) response
is terminal_error / unknown and invokes failure+refund. Terminal body status
is recognized as terminal_error / 4xx, retryable_error as transient / 5xx.
Other unrecognized bodies are not fulfillment successes. 3xx is terminal and
never followed, 4xx terminal except 408/429, 5xx transient. TLS failure is
terminal. DNS/connection/timeout failures are transient. Categories reuse
webhook vocabulary (timeout, connection, tls, dns, 3xx, 4xx, 5xx, unknown), plus
ssrf_blocked and budget/busy pending metadata, without raw exception strings.
Retries use bounded randomized exponential delays, stable idempotency and a
fresh DNS check. Retry-After and arbitrary supplier errors are not trusted.

## Transaction and failure handling

1. The existing order transaction commits debit, ledger, order and creation/
   pending events exactly as before. No external HTTP runs inside it.
2. Router performs network-only dispatch outside transactions. A completed
   result is handed back to the originating request for local lifecycle work.
3. Fulfilled content reuses the 16.4 completion service: forced encryption,
   safe_write_delivery, completed master status, updated timestamp and strict
   delivery.completed emission are one pgcompat.atomic business unit.
4. Confirmed terminal/exhausted failures call handle_post_debit_failure with
   `fulfillment_error: <allowlisted category>`. Existing refund cap, refund
   records, ledger references and stock guards are reused. No duplicate refund
   engine is added. Partial previous refunds remain capped. If that engine
   crashes after persisting Failed but before refund/stock commits, report the
   terminal state without claiming refund success; its existing owner Fail
   retry/recovery path must finish missing effects without supplier redispatch.
5. Owner fail/cancel/deliver and fulfillment finalization use the same existing
   lifecycle claim key; no claim/transaction spans the network call. Already
   terminal or delivered state is rechecked before applying effects.
6. A supplier success followed by local write/encryption/outbox failure rolls
   back completion and remains pending, with the original pending event. It is
   NOT falsely reported delivered or speculatively refunded. Original supplier
   result must remain recoverable by the agreed idempotency/order lookup.

## Latency and resource limits

The default **supplier dispatch wait** is 5 seconds (maximum configurable 10),
not a claim that every database/API operation completes within ten seconds.
Connect/read timeouts alone do not bound OS DNS or slow streaming, so a bounded
network-only daemon thread is used. On budget expiration the request stops
waiting and returns pending; stop signaling prevents subsequent retries/writes.
A late result never writes delivery or refunds. At most 16 network calls may
remain outstanding per process; saturation returns pending without new HTTP.
Late/ambiguous upstream outcomes must be reconciled, not resent on API replay.
The dedicated 16.7 worker separately reconciles these pending outcomes.

Database locks/refund work and ordinary request overhead are outside the
network budget and still use the existing timeout/transaction policy. Budget
expiry cannot cancel an already accepted supplier operation. The supplier
idempotency/recovery contract and monitoring pending orders are essential.

## SSRF / TLS / logging

All A/AAAA answers must be global public IPs. Any private/non-global answer is
rejected. Immediately before each call, DNS is re-resolved, one checked IP is
pinned to a requests HTTPS connection pool, and original Host/TLS SNI/certificate
hostname validation are retained. Requests cannot re-resolve to an unchecked
host between validation and connect. TLS verification is always on, redirects
are off, environment proxies/netrc are disabled, and adapter retries are off
(the client owns its bounded retry policy).

No raw upstream error/body/config is logged. Thread-local transport logger
filters redact credential/endpoint/path values and suppress raw tracebacks;
a quiet HTTPS connection class prevents global http.client debugging from
printing request headers/body. Observability also treats fulfillment_config, api_key and HMAC fields as
sensitive. Error results contain only static allowlisted categories.

## Verification / rollout

Run `python -m unittest tests.test_phase16_fulfillment_wiring -q` in an isolated
test environment. The suite uses a real local HTTPS stub with a generated test
CA; only transport DNS/IP/CA boundaries are redirected to loopback. Production
code has no localhost exception or verify=False. Financial and delivery
assertions exercise real routes, ledger, failure engine, completion service,
webhooks, encryption and actual authenticated GET delivery.

Re-run Phase 12 final, Phase 15 finance, Phase 16.3–16.5, wallet/WebAdmin and
existing regressions. Stage against your real public HTTPS provider, PostgreSQL
and production topology before rollout. Never use production keys/data in tests.
See `supplier_fulfillment.md` and `RAILWAY_RESELLER_API.md` for the dedicated
worker, supplier status contract and deployment configuration.
