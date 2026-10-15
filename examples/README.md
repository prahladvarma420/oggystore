# Reseller API client kit

Local reference implementations, not a managed webhook service. No application
API/worker code is changed by these examples. Use a **separate inbox file and
signing secret for each webhook/environment**. Never share a test inbox or key
with production. The default event handler only logs a safe event type; replace
it with your own validated, idempotent business logic.

## Run a receiver locally

Python 3.13 with Flask from the project's requirements:

```sh
python -m pip install -r requirements.txt pyyaml
# Set WEBHOOK_SECRET privately to the secret returned on webhook creation or
# rotation; never paste it into chat, source control, command history or logs.
# Set INBOX_DB to a private, persistent SQLite file outside the source checkout.
# Set a restrictive umask before creating the file (POSIX):
umask 077
python examples/webhook_receiver.py
```

JavaScript uses Node.js **24+**, built-in `node:sqlite`, and Express. Install the
locked example dependencies (no production secrets required):

```sh
npm ci --prefix examples --ignore-scripts
umask 077
# Configure WEBHOOK_SECRET / INBOX_DB privately as above.
node examples/webhook_receiver.js
```

Both bind to `127.0.0.1`, port 8080 by default (`PORT` overrides). Both accept
`POST /webhook`, at most 256 KiB, uncompressed UTF-8 JSON. A secret shorter than
16 characters is rejected; use the actual high-entropy secret issued by the API.
Python defaults to `receiver-python.sqlite3`, Node to `receiver-node.sqlite3`;
configure a persistent protected path for non-disposable use. Do not commit the
SQLite file/WAL/SHM or filled environment exports. A missing parent directory or
unwritable DB fails startup; request-time storage failures return 503, not 200.

Flask's built-in server is for **local testing only**. For deployment, use a
production WSGI host for Python or a supervised Node process, TLS at your edge,
restricted DB permissions, monitoring, backups and a persistent local volume.
SQLite WAL is for local filesystems, not a shared network filesystem. For a
multi-host service, adapt the inbox/lease pattern to an appropriately durable
shared queue/database. Do not expose a local debugging server publicly.

The sender refuses HTTP, localhost/private-IP URLs and redirects. To use the
real `POST /api/v1/webhooks/{webhook_id}/test`, register an operator-controlled
**public HTTPS endpoint** forwarding unchanged bytes to this receiver. Configure
your own verified public URL; no tunnel hostname or production endpoint is
embedded here. Receiver unit tests use loopback locally and do **not** weaken
sender SSRF checks. The test endpoint sends `test.ping` synchronously; check the
returned `data.test.delivered`/status, then the receiver inbox. `test.ping` is
not a valid subscription choice and is not created through the business outbox.

## Exact signature contract

1. Capture the **raw request bytes before parsing** (`request.get_data` in Flask;
   route-specific `express.raw`, before any JSON middleware, in Express).
2. Read `X-Oggystore-Timestamp` (canonical Unix epoch seconds) and
   `X-Oggystore-Signature-256` (`sha256=` plus 64 lowercase hex characters).
3. Enforce the same inclusive **±300-second** window as the project signer. Keep
   receiver clocks synchronized; old and excessive-future timestamps fail.
4. Sign `timestamp_ascii + b"." + original_body_bytes` using HMAC-SHA256 and the
   secret's UTF-8 bytes. Compare using `hmac.compare_digest` or
   `crypto.timingSafeEqual`. Reject missing/malformed/incorrect signatures with
   401; do not acknowledge them as delivered.
5. Only after verification, decode UTF-8 and validate the object, supported event
   type and event ID. Malformed/unsupported events return 400. The
   `X-Oggystore-Event` metadata must agree with the signed body's event type.
   `X-Oggystore-Delivery` is attempt metadata, **not** the deduplication key.

A proxy must not pretty-print JSON, normalize Unicode/line endings, decompress,
re-encode or otherwise change the signed entity bytes. Equivalent JSON is not
equivalent HMAC input. These examples refuse compressed bodies; do not install
an earlier parser or auto-decompressing proxy in front of signature verification.
Only timestamp and body bytes are HMAC-bound. Unsigned metadata headers do not
provide independent authentication.

Business event IDs are positive integers (accepted up to JavaScript's safe
integer bound; integral JSON forms `42`, `42.0` and `4.2e1` normalize to the same ID); ping IDs are strings such as `ping-...`. Nonempty bounded safe
strings are also accepted. Boolean, missing or malformed IDs are rejected.
Both `event` and `event_type` are supported, and must agree when both exist.
Never deserialize a verified event into an arbitrary command or execute body
text. Add application-specific validation before your business handler acts.

## Durable acceptance, retries and idempotency

Both examples implement a **persistent SQLite inbox**, not an in-memory set:

- A unique `event_id` is inserted transactionally with the payload, fingerprint,
  and queued state. HTTP 200 is returned **after that commit**, without awaiting
  business processing. Duplicates with the same event ID return HTTP 200 and
  `duplicate: true`, including after a receiver restart.
- The sender changes `delivery_id` **in the body** on retries. The stable
  fingerprint excludes that one attempt field; canonical JSON ignores object
  key order/whitespace. Reusing an ID with different stable content returns 409
  without overwriting the stored event. Keep one DB per receiver/environment;
  the examples are alternatives, not cross-language shared-file deployments.
- A background worker claims one job with a 60-second lease/token. Handler work
  is outside the insertion transaction/request. A crashed processing claim is
  recovered once its lease expires; queued work resumes after restart.
- Processor exceptions are sanitized, saved as `processing_failed`, and retried
  locally with bounded exponential delay (2, 4, 8, 16 seconds; cap 60). After five
  handler failures, the row is retained as `failed` for operator review.
- A post-ACK error cannot trigger a sender retry: the receiver now owns recovery.
  Monitor queue/DLQ age. After fixing your handler, manually requeue failed jobs:

```sh
python examples/webhook_receiver.py --retry-failed
# OR, for the Node receiver's own configured database:
node examples/webhook_receiver.js --retry-failed
```

The CLI uses `INBOX_DB`; it does not expose an HTTP admin/retry endpoint. Check
that the failed-job retry is appropriate before rerunning real side effects.

**Exactly-once business effects are NOT guaranteed by receipt deduplication.**
A crash after an external side effect but before marking the inbox row done can
repeat the handler. Use the event ID as your business idempotency key, and make
local effects/inbox completion transactional or use an idempotent downstream
service/outbox. Keep handlers bounded below the lease or implement lease renewal
before introducing long jobs. A stale token cannot finalize a newly claimed job,
but it cannot undo an already executed external effect. For Node, do not block
the event loop with CPU-heavy handlers; use worker threads or an external worker.
For Python, manage one worker lifecycle per production process; the example
factory starts a daemon thread, not a separate hosted worker service.

The default processor has an explicit `test.ping` branch and otherwise logs only
a supported event type. It does **not** fulfill an order, fetch delivery secrets,
credit money, or log payloads, signatures, buyer emails or signing secrets.
Protect stored payloads/backups and apply an appropriate retention policy without
deleting deduplication keys while replay is possible. SQLite payloads are not
encrypted by this example; use encrypted storage/access controls as needed.

The platform worker remains unchanged: at-least-once attempts, max 10 by default,
full-jitter exponential backoff (base 60 seconds, cap 3600), transient/network/
408/429/5xx retryable, redirects and terminal 4xx non-retryable. Invalid signatures
must be corrected/rotated then manually retried through the API as appropriate;
successful ACKs must not depend on a slow business callback.

## Generated Postman collection

```sh
python tools/generate_client_kit.py
python tools/generate_client_kit.py --check
```

The generator reads `docs/openapi.yaml`, not a second endpoint list. Import
`OGGY-Reseller-API.postman_collection.json` and one of the separate blank
`OGGY-test.postman_environment.json` / `OGGY-production.postman_environment.json`
files. In your private Postman environment, fill:

- `base_url`: your verified public v1 base URL, **including `/api/v1`**, no trailing
  slash. Nothing is copied from OpenAPI's production/staging server addresses.
- `key_id` and `secret`: the credential pair for that environment only. The
  collection sends `Authorization: Bearer {{key_id}}.{{secret}}`.
- Public resource/path/request variables as needed. Optional query parameters
  are disabled until selected. Template values must be configured before use.

A pre-request check prevents requests with blank connection/credential fields.
Test and production environment files are intentionally blank: choosing a file
alone does not create server-side sandbox isolation. Use the matching deployment
and credentials under the existing API environment rules. Never export/share
filled credentials or submit real payment/order operations accidentally.

All documented operations are included, with inherited templated authentication.
Request bodies use explicit OpenAPI examples when present, sanitized to variables
for URLs, IDs, emails/credentials; otherwise required schema fields produce a
minimal editable JSON body. This is **not** a promise that a template can run
unchanged: fill valid account-specific values and optional gated fields such as
`buyer_email` when required. Missing examples are never silently omitted.

`postman-collection-v2.1.schema.json` is the vendored official Postman v2.1 schema,
from the collection's standard schema URL. Automated tests validate against that
schema using Ajv and compare every method/path with OpenAPI. A real Postman GUI
import is a separate manual deployment check, not claimed by schema validation.

## Code-derived error catalog

Section 17 of `RESELLER_API.md` and `error_codes.json` are generated from AST reads
of code-bearing API/service errors, request-security tuples and HTTP mappings.
No API app or database is imported by the generator. Transparent service-error
adapters are recognized; unknown dynamic error producers fail generation rather
than disappearing. Internal service-only errors are labeled (not advertised as
new public routes). Source messages show `{value}` instead of runtime input.

Run `--check` after any API error or OpenAPI change. The tests also inject new
literal/dynamic error sites to prove that drift is detected/fails closed. If a
new adapter is genuinely needed, inspect it and extend the generator explicitly.

## Verification

```sh
npm ci --prefix examples --ignore-scripts
python tools/run_tests.py --filter phase17_client_kit
python tools/generate_client_kit.py --check
python tools/run_tests.py
```

The isolated runner shares installed example Node dependencies via a controlled
`NODE_PATH`, just as Python uses installed site-packages; node_modules itself is
not copied into disposable suite sources. Do not inherit untrusted NODE_OPTIONS.
Tests exercise actual loopback Flask/Express receivers using the project's signer,
including raw-byte tampering, timestamp boundaries, durable duplicate/crash/error
handling and nonblocking ACKs. Node 24+ absence is reported as explicit skips,
never as executed JavaScript verification. CI sets up Node and runs `npm ci`.
