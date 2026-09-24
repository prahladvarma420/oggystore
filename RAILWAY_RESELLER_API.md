# OGGY Reseller API — Railway Service

This adds one independent HTTP runtime to the existing Railway project. It does **not** create another database or duplicate product, stock, order, payment, wallet, delivery, or webhook systems.

## Verified architecture

```text
                 Postgres-SG
                      |
      +---------------+---------------+
      |               |               |
   worker         web-admin      reseller-api
 python bot.py     Flask UI       Flask API
```

All three application services deploy the same GitHub repository and use the same existing `Postgres-SG` database through `DATABASE_URL`.

## New service settings

| Setting | Value |
|---|---|
| Service name | `reseller-api` |
| Source | Same GitHub repository as `worker` and `web-admin` |
| Repository | `github.com/prahladvarma420/oggystore` |
| Branch | The same production branch currently used by the other services |
| Root directory | `/` (repository root; leave Railway's field blank if blank means root) |
| Build command | Leave blank; Railpack detects Python, installs `requirements.txt`, and applies `railpack.json` |
| Start command | `gunicorn --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT reseller_api.app:app` |
| Health path | `/health` |
| Restart policy | On failure (Railway default is acceptable) |

Do not add or change `PORT`; Railway injects it dynamically.

## Required variables

Add these only to the new `reseller-api` service:

```text
DATABASE_URL=${{Postgres-SG.DATABASE_URL}}
RESELLER_API_ENVIRONMENT=production
RESELLER_API_REQUIRE_POSTGRES=1
RESELLER_API_HSTS=1
RESELLER_API_TRUST_PROXY=1
DATA_ENCRYPTION_KEY=<reference or copy the exact existing value used by worker/web-admin>
```

Optional:

```text
RESELLER_API_MAX_BODY_BYTES=1048576
TIMEZONE=Asia/Kolkata
```

`BOT_TOKEN`, Web Admin credentials, payment API keys, wallet addresses, and Telegram admin variables are not required by the current API foundation. Never paste secret values into code or deployment logs.

`RESELLER_API_REQUIRE_POSTGRES=1` makes startup fail closed if the shared PostgreSQL reference is missing or malformed. This prevents Railway from silently creating a local `shopbot.db` for the API service.

## Create the service

1. Open the existing Railway project **Oggy Store Bot** and the `production` environment.
2. Click **New** → **GitHub Repo**.
3. Select `prahladvarma420/oggystore`, the same source repository used by `worker` and `web-admin`.
4. Rename the new service to `reseller-api`.
5. In **Settings → Source**, select the same production branch. Keep the root directory at repository root.
6. In **Settings → Build**, leave the custom build command blank. Railway/Railpack will install the root `requirements.txt` and use `railpack.json`.
7. In **Settings → Deploy**, set the custom start command exactly to:

   ```text
   gunicorn --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT reseller_api.app:app
   ```

8. In **Variables**, add the required values above. For `DATABASE_URL`, use the Railway reference `${{Postgres-SG.DATABASE_URL}}`; do not copy a public proxy URL and do not create another PostgreSQL service.
9. Confirm Railway resolves the reference without revealing its value. The API logs should report only `database=postgresql`, never the URL.
10. In **Settings → Healthcheck**, set path `/health`. Use a 10-second timeout if Railway exposes a timeout field. The endpoint returns HTTP 200 without querying PostgreSQL.
11. Deploy the service and inspect logs for successful Gunicorn worker boot and the sanitized API initialization message.

## Deployment verification

After Railway provides a generated service domain, run:

```bash
curl -i https://<reseller-api-generated-domain>/health
curl -i https://<reseller-api-generated-domain>/health/db
curl -i https://<reseller-api-generated-domain>/api/v1/
```

Expected results:

- `/health`: HTTP `200` and `{"service":"reseller-api","status":"ok"}`.
- `/health/db`: HTTP `200` and the same non-sensitive status shape. HTTP `503` means the shared database is unavailable or `DATABASE_URL` is wrong; the response never contains driver details.
- `/api/v1/` without a Bearer key: HTTP `401` with the existing structured `authentication_failed` response, proving the route is initialized and still protected. If the global Web Admin API switch is disabled, `503 api_unavailable` is expected instead.
- `/api/v1/products`, `/api/v1/orders`, `/api/v1/balance`, and `/api/v1/webhooks`: HTTP `404`; these were intentionally not implemented.

For an authenticated v1 test, use a test-environment key only against a service configured with `RESELLER_API_ENVIRONMENT=test`. Never paste production secrets into Railway logs or shell history.

## Dedicated domain: api.ggm25.shop

Do not change the existing `ggm25.shop` Web Admin domain.

1. Open `reseller-api` → **Settings → Networking → Custom Domain**.
2. Enter `api.ggm25.shop`.
3. Railway will display the exact DNS target and, if required, a verification record.
4. At the DNS provider for `ggm25.shop`, create the record Railway specifies—normally a `CNAME` with host/name `api` pointing to Railway's displayed target. Do not proxy it until Railway finishes certificate validation unless the DNS provider/Railway instructions explicitly allow proxying.
5. Add any Railway-provided TXT verification record exactly as shown.
6. Remove conflicting `A`, `AAAA`, or `CNAME` records for the `api` host only. Do not modify records for the apex domain or Web Admin.
7. Wait for DNS propagation and Railway TLS issuance, then verify:

   ```bash
   curl -i https://api.ggm25.shop/health
   curl -i https://api.ggm25.shop/health/db
   curl -i https://api.ggm25.shop/api/v1/
   ```

## Security checklist

- Flask debug mode is disabled.
- Gunicorn is the production WSGI server.
- No wildcard CORS headers are emitted.
- Health responses contain no environment values, paths, credentials, API keys, or stack traces.
- Unexpected errors use the existing sanitized error envelope.
- `DATABASE_URL` is consumed by the shared `database/pgcompat.py` layer and is never logged.
- `/api/v1/` retains the existing global kill switch, authentication, authorization foundation, rate limiting, request IDs, redaction, and security headers.
- `/health` is public and process-only; `/health/db` is public but runs only `SELECT 1` and returns generic status.

## Rollback

1. Remove the custom domain from `reseller-api` (if attached).
2. Delete only the `api` DNS record created for `api.ggm25.shop`.
3. Stop or delete only the Railway `reseller-api` service.
4. Revert the infrastructure commit if desired.

The existing `worker`, `web-admin`, and `Postgres-SG` services require no command, variable, schema, or domain changes for rollback.
