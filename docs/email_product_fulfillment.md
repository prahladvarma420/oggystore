# Private email-gated API fulfillment — Prompt 16.8

## Request and pricing contract

`buyer_email` is accepted on order creation and quote, required when the resolved
product is identified by `is_m365_product` or `is_canva_product`, forbidden for
all other products. Missing gated email gives 422 `buyer_email_required`; a
present malformed/non-string/null/empty/overlong value gives 400
`invalid_parameter`. Non-gated presence gives 400 with exactly `not accepted
for this product`. Responses/errors never echo the supplied email.

Validation is a strict RFC-5322-style ASCII dot-atom subset: at most 254
characters total, local part <=64, no leading/trailing/consecutive local dots,
valid domain labels and alphabetic TLD, no whitespace/control characters,
display names, quoted local parts, domain literals or SMTPUTF8. Case is retained;
no implicit trimming/relabeling. A private address included in public
client_order_id is rejected with a static 400 error, preventing indirect
plaintext persistence/event/response leakage of the new private field. Never
put personal data into public identifiers or public catalog names.

The existing Telegram M365/Canva purchase path already rejects quantity !=1;
API validation preserves that one-recipient rule. Gated quotes report
max_per_order 1 and add only requires_buyer_email true. Other quote shapes and
all pricing arithmetic remain unchanged. Quote still requires valid email;
it never enqueues, stores email, reserves stock, debits or consumes quota.

Valid client_order_id replay with a changed email returns the ORIGINAL order:
no buyer rewrite, second queue row, debit or supplier/browser call. The Phase
12 replay validation/contention paths are preserved rather than bypassed.

## Existing queues and existing browser implementation

No schema change is added. Private emails need persistence because HTTP returns
before fulfillment and lane contention/restarts require retry. Existing
m365_invites.email / canva_invites.email columns are reused, **force-encrypted**
with the existing encrypt_text helper and shared DATA_ENCRYPTION_KEY. Existing
queue initialization is done outside the order transaction; enqueue itself is
inside the same debit/order/outbox atomic block and never commits independently.
Failed enqueue rolls back the order/debit and restores reserved stock. No new
email column/table/store is introduced; normal customer queue helpers are not
changed to plaintext/decrypt API rows.

API rows use user_id 0 plus the authoritative orders.source reseller_api check,
and separate status values in the SAME existing invite tables. The API
order's delivery stays NULL despite static product text or product stock codes:
these gated plans use the same manual stock-counter model as Telegram. The
HTTP creation response is processing/pending and never runs Chromium or asks a
Telegram question. No ordinary order/quote response gains an email or masked
email field.

The existing bot maintenance loop drains a bounded batch from both queues using
a durable order-ID scan cursor. The customer M365 queued flush remains unchanged.
API work uses the **same bot-process pw_lane** as M365 customer invites, Canva
customer invites and keepalive. It does not execute in the API process or the
separate supplier worker, which would otherwise bypass the process-local lane.

Acquire the existing lane non-blockingly **before** creating a browser thread.
When busy, no browser thread is stacked. After acquiring, M365 uses the existing
warranty-aware failover driver; Canva uses the same existing retry driver,
extracted as a locked helper without changing its customer retry behavior.
API processing bypasses interactive/customer/admin email Telegram messages.

The async wait is bounded at 300 seconds. Timeout does NOT free the lane while
an abandoned thread is still executing: that thread releases only on real exit,
so later maintenance cannot stack another Chromium worker behind it. API calls
remain nonblocking regardless of that long-running work.

## Completion, failure and recovery

- A real successful invite first persists api_sent in the existing queue.
  Local completion then uses the established 16.4 helper, owner lifecycle claim,
  force encryption, safe_write_delivery, completed status/updated timestamp and
  strict delivery.completed event in one atomic block. Stored/returned delivery
  is generic inbox/acceptance instructions, never the email or proof screenshot.
- If local delivery/outbox persistence fails, the durable sent receipt resumes
  local completion WITHOUT sending a second invitation. Repeat/concurrent
  attempts cannot overwrite delivery or duplicate completion events.
- Missing/expired M365 sessions, missing Canva session or a busy lane keep the
  order pending. Queueable retries are deferred at least 60 seconds and attempted
  on later maintenance. The existing bot scheduling cadence is hourly with first
  maintenance at 60 seconds; delivery latency follows that cadence and existing
  customer/keepalive activity, not a new instant-processing promise.
- Confirmed nonqueueable failure records api_rejected before invoking the
  existing 16.7 failure wrapper / post-debit engine. A temporary owner claim
  conflict retries financial settlement, never the browser invitation. Partial
  admin refund caps, refund records/ledger refs, stock and failure-event guards
  are reused, not implemented again.
- Pending jobs at FULFILLMENT_MAX_AGE_SECONDS (default 24h) are failed/refunded
  on maintenance even when the browser lane remains permanently busy. Age and
  sent/rejected local recovery are checked BEFORE lane acquisition. PostgreSQL
  created timestamps must be UTC; the existing aware shop clock is reused.
- If the financial engine crashes after Failed status but before refund/stock,
  its durable automatic failure intent is repaired by the existing dedicated
  16.7 fulfillment worker. Keep that worker running alongside the bot.
- Browser providers offer no reliable transaction/idempotency receipt covering
  process death after remote acceptance but before api_sent persists. A stale
  api_running job becomes api_uncertain; it is NOT blindly reinvited. An owner
  must check the provider and use existing owner delivery/fail/cancel workflows.
  The maximum-age refund rule still applies. This explicitly avoids falsely
  promising exactly-once remote invitation across an unobservable crash.

Job claims use email-invite:order_id, unique ownership tokens and a 900-second
stale TTL; local terminal actions reuse existing admin-order-lifecycle claims.
No external browser/HTTP work occurs in a database transaction. Authoritative
terminal/delivery checks prevent queued work from completing an owner-failed,
cancelled or manually delivered order. If an owner refunds while a remote invite
is already in flight, a provider side effect cannot be undone; later local
completion is still refused. Review in-flight jobs before manual intervention.

## Privacy and deployment

API field is never copied into order columns, ledger descriptions, business
errors, analytics, CSV or event builders. Observability redacts email field keys
and free-text addresses. API browser execution has a thread-local privacy
context that suppresses diagnostic log records/tracebacks and replaces legacy
service print breadcrumbs; normal Telegram logging remains unchanged. Raw
provider messages/screenshots are discarded and API screenshot globals cleared.

Playwright driver subprocess debug output bypasses Python filters, so API
browser work is refused while DEBUG, PWDEBUG or DEBUGP is nonempty. Leave them
unset/empty in the bot runtime; otherwise jobs remain pending until corrected
or age-refunded. Do not add debug/tracing/screenshot uploads that expose buyers.
Existing customer logging/interactive messages are not globally rewritten.

Deploy the API and existing bot from the same code with the exact shared
DATA_ENCRYPTION_KEY and PostgreSQL database. Keep **one Telegram bot process**:
pw_lane is process-local, as in the existing architecture, not a new global
cross-host browser semaphore. Do not launch API email jobs in the supplier/API
service or duplicate bot replicas. Existing provider sessions, Playwright /
Chromium installation and warranty groups are required for actual invitations.
No supplier credentials/email data are exposed through API configuration.

Run the existing 16.7 fulfillment worker for financial intent recovery and the
webhook worker for committed outbox delivery. No extra process/Procfile entry is
added for email jobs. Stopping the bot leaves email orders pending; monitor
backlog and arrange manual settlement instead of assuming the API alone sends
invites. Ordinary settled email rows retain encrypted PII under the existing
invite-history retention policy; no automatic data deletion/production cleanup
is introduced.

Local tests mock only the existing browser lane/provider automation boundary;
order validation, queues, pricing, wallet/refund/stock, encrypted delivery,
outbox and the authenticated delivery endpoint are real. Staging with actual
Microsoft/Canva sessions and PostgreSQL remains required; no real invitations
or production operations occurred during development.
