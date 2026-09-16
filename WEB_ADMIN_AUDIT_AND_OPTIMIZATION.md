# OGGY Store Web Admin — Audit and Optimization Report

## 1. Project overview

This is a Python Telegram commerce bot with a Flask web administration SPA. The bot and web admin share the same persistence and domain functions. SQLite is the default database; `database/pgcompat.py` translates the existing SQLite-style SQL to PostgreSQL when `DATABASE_URL` is configured.

**Stack:** Python 3, python-telegram-bot 22.7, Flask 3.1, vanilla JavaScript, CSS, Chart.js, SQLite/PostgreSQL, Web3/BSC, TronGrid, Binance APIs, Playwright, Fernet encryption, Gunicorn/Railway.

**Execution model:** `bot.py` runs Telegram polling and scheduled jobs. `web_admin/app.py` runs Flask separately. Both point at the same database. Payment checkers credit deposits and delivery handlers reserve encrypted stock and create orders.

## 2. Project structure

- `bot.py` — bot bootstrap, command/callback registration, payment/background jobs.
- `config.py` — environment configuration, payment endpoints, timing and store constants.
- `database/db.py` — schema, migrations, persistence/domain helpers and shared thread-local connections.
- `database/pgcompat.py` — SQLite-to-PostgreSQL SQL/connection compatibility layer.
- `handlers/` — customer conversation and account flows.
- `products/` — catalog, cart/checkout, delivery and warranty behavior.
- `payments/` — deposit creation and BEP20/TRC20/Binance verification.
- `admin/` — Telegram admin panel and commands.
- `services/` — Canva and Microsoft 365 fulfillment automation.
- `tasks/` — marketing, broadcasts, reminders and maintenance.
- `utils/` — encryption, backups, UI, timezone, logging and delivery formatting.
- `web_admin/app.py` — Flask middleware, authentication, 79 routes and admin API controllers.
- `web_admin/templates/` — login and SPA shell.
- `web_admin/static/js/app.js` — vanilla-JS state, API client and all admin views/forms.
- `web_admin/static/css/style.css` — responsive theme and component styling.
- `tools/` — migration, credential, backup and service-login utilities.

The reviewed archive contains 65 files, 50 Python files and approximately 25,000 Python lines. No automated test suite was present.

## 3. Database and domain model

Core tables are `users`, `products`, `categories`, `product_stock`, `product_price_tiers`, `orders`, `deposits`, `processed_txs`, `coupons`, `coupon_redemptions`, `user_coupons`, `tickets`, `referrals`, `withdrawal_requests`, `restock_notifications`, `scheduled_broadcasts`, `message_templates`, `settings`, `admins`, `admin_logs`, `stock_op_log`, `bundles`, and `bundle_items`. Specialized fulfillment adds Canva invites, M365 invites/sessions, gift codes and replacements. Web authentication adds `web_admins`, `web_sessions`, and `web_login_log`.

Delivery values, wallets and service credentials use `utils.crypto` when `DATA_ENCRYPTION_KEY` is configured. The application remains backward compatible with plaintext legacy values, but reports encryption as disabled when the key is absent.

## 4. Web admin architecture and data flow

The browser loads the authenticated shell from `/`, then `app.js` renders one view at a time and calls JSON endpoints. Flask validates the session, idle timeout, session revocation, IP/rate controls and CSRF token. Controllers execute SQL through short-lived `q`/`ex` connections or call shared `database.db` domain helpers. JSON is escaped before HTML insertion, and destructive actions generally use confirmation dialogs.

### Admin areas

| Area | Frontend | API/controller | Main tables/actions |
|---|---|---|---|
| Login/security | `login.html`, security view | `/login`, `/api/security*`, `/api/sessions*` | `web_admins`, `web_sessions`, `web_login_log`; password + mandatory TOTP, recovery codes, session revocation |
| Dashboard | `viewDashboard`, Chart.js | `/api/dashboard`, `/api/analytics` | orders, users, products, deposits, tickets, withdrawals; KPIs, trends, top products, low stock |
| Products/categories | `viewProducts`, product/category modals | `/api/products*`, `/api/categories*` | products, categories, stock, tiers; CRUD, visibility, featured, flash sales |
| Inventory | `manageStock`, `viewRestock` | stock/tier/restock endpoints | product_stock, stock_op_log, restock_notifications; add/delete codes, account blocks, offers |
| Orders | `viewOrders` | `/api/orders*` | orders, users, deposits, replacements; search/filter/page, refund, resend |
| Users | `viewUsers`, detail modal | `/api/users*` | users, orders, deposits; search/page, balance adjustment, block/unblock |
| Payments | `viewDeposits`, withdrawals | `/api/deposits`, `/api/withdrawals*` | deposits, withdrawal_requests; status/method summaries and approval flow |
| Marketing | coupons/broadcast/template views | corresponding `/api/*` routes | coupons, scheduled_broadcasts, message_templates; create/toggle/send/edit |
| Support | tickets view | `/api/tickets*` | tickets/users; filter, reply, close |
| Settings/system | settings/admin/log/backup views | `/api/settings*`, `/api/admins*`, `/api/logs*`, `/api/backup*` | settings, Telegram admins, admin logs and backup files |
| Profit/reporting | profit/dashboard views | `/api/profit`, `/api/analytics`, `/api/export/*` | products, stock, orders, users, deposits |

## 5. Problems found before changes

### Security

- **Critical:** both first-login and in-panel TOTP setup sent the complete `otpauth://` URI, including the TOTP seed, to `api.qrserver.com`. A third party could possess the second factor.
- The `admin` role could reach sensitive owner operations such as backup download, customer/order/product CSV exports, Telegram-admin management and full logs. Account-management checks existed, but authorization was inconsistent and route-local.
- TOTP seeds were written directly to `web_admins.twofa_secret` even when application encryption was available.
- The process-local rate-limit dictionary had no size bound, allowing unbounded growth from many source addresses.
- Deployment remains unsafe if HTTPS, a fixed `WEB_ADMIN_SECRET`, `DATA_ENCRYPTION_KEY`, trusted-proxy settings or strong credentials are omitted.
- File upload is limited to 8 MB and logo extensions are restricted, but content-level image verification is still limited.

### Performance

- `/api/analytics` executed roughly 56 daily aggregate queries plus per-product stock queries.
- `/api/profit` executed stock and order queries per product (N+1 pattern).
- `/api/restock` executed a stock query per result.
- Dashboard seven-day revenue executed seven separate scans/connections.
- Several high-frequency filter/join columns lacked indexes.
- Products, coupons, logs and deposits can still return large result sets; products/coupons need server pagination as catalogs grow.

### UX/UI

- Seventeen flat sidebar items had weak hierarchy.
- URL/back navigation did not preserve SPA location.
- Fetch failures were often converted into vague “No data” states; network and HTTP errors were not consistently surfaced.
- Modal focus was not moved into dialogs; Escape did not close dialogs; several controls lacked explicit accessible labels.
- Tables use horizontal overflow correctly, but dense action rows remain challenging on small screens.
- Loading, empty states, toasts, filters, order/user pagination and destructive confirmations already existed and were preserved.

### Code quality/architecture

- `web_admin/app.py`, `app.js`, `database/db.py`, and Telegram admin handlers are large monoliths.
- SQL and business rules are split between Flask controllers and `database.db`; transaction boundaries are not consistently explicit.
- Broad exception swallowing appears frequently and can hide failed migrations, notifications and maintenance operations.
- Route input validation is inconsistent; some numeric casts can become generic 500 responses.
- Templates are populated with string replacement rather than Jinja rendering.
- No automated tests exist; `verify_patch.py` is a syntax/structure verifier, not behavioral coverage.
- Chart.js is CDN-hosted, so analytics charts depend on external availability.

## 6. Prioritized optimization plan

### P0 — Critical

1. **Keep TOTP secrets local.** Replace external QR generation with server-side QR data URIs; encrypt stored seeds. Files: `web_admin/app.py`, `app.js`, `requirements.txt`. Risk: low; fallback manual key remains. DB migration: no schema change. API: setup response replaces `otpauth` with `qr`. Frontend/backend: yes.
2. **Enforce owner authorization centrally.** Add reusable owner guard to backups, exports, logs, Telegram-admin and web-admin management. Files: `app.py`, `index.html`, `app.js`. Risk: medium because staff lose access to sensitive operations by design. DB: no. API behavior: unauthorized roles now receive 403.
3. **Production configuration gate (remaining).** Fail production startup when fixed session/encryption secrets or HTTPS/proxy settings are unsafe. Risk: medium; needs deployment coordination.

### P1 — High

1. **Remove aggregate N+1 queries.** Batch dashboard, analytics, profit, restock and stock counts. Files: `app.py`. Risk: low/medium; preserve response shapes.
2. **Add justified indexes.** Index order dates/users/products, deposit status/users, stock product/status, ticket status and withdrawal status. File: `database/db.py`. Risk: low; idempotent migration, minor write/storage overhead.
3. **Improve navigation and failure states.** Group sidebar, add hash history, owner-aware controls, explicit network/HTTP errors and dialog keyboard behavior. Files: templates/JS/CSS. Risk: low.
4. **Add real integration tests (remaining).** Authentication, authorization, checkout/payment idempotency, stock reservation/refund and SQLite/Postgres parity.

### P2 — Medium

- Split Flask APIs into blueprints and extract service/repository layers incrementally.
- Split the JS monolith into feature modules and reusable table/form/modal primitives.
- Add server pagination to products, coupons, deposits and logs without changing current default contracts.
- Standardize validation and JSON error envelopes; add a safe global exception handler with correlation IDs.
- Replace silent broad catches with structured logging where failure matters.
- Improve compact mobile table/card rendering and focus trapping in dialogs.

### P3 — Nice to have

- Self-host Chart.js and other browser assets.
- Saved filters, bulk catalog/inventory actions and audit-log retention controls.
- Background queue with persisted retries for broadcasts and external fulfillment.
- Query timing/health telemetry and database migration versioning.

## 7. Changes implemented

### Local, encrypted TOTP enrollment

**Before:** TOTP URIs were sent to a public QR service and seeds were stored plaintext. **After:** QR PNGs are generated by Flask and returned as data URIs; manual enrollment remains as fallback; new seeds pass through application encryption. **Why:** prevents second-factor disclosure and improves at-rest protection. **Impact:** backend/API/frontend/security; no schema change.

### Central owner-only protection

**Before:** the broad `admin` role could access exports, backups, logs and Telegram-admin operations; web-admin routes repeated inline checks. **After:** `owner_required` returns a consistent 403 and is applied to 16 sensitive routes; owner-only navigation and export actions are hidden for staff. **Why:** least privilege and defense in depth. **Impact:** backend/API/frontend/security; no database change.

### Batched analytics and reporting

**Before:** analytics used approximately 56 daily queries plus N+1 stock queries; profit and restock were N+1. **After:** grouped queries build maps in memory while retaining existing response payloads and legacy order-name fallback. Dashboard seven-day revenue is one grouped scan. **Why:** substantially fewer connections/scans, especially important with remote PostgreSQL. **Impact:** backend/performance only; API response compatible.

### Query indexes

**Before:** common date, user, product and status lookups could scan entire tables. **After:** nine idempotent indexes are created in the existing migration path. **Why:** faster dashboards, histories, joins and operational queues. **Impact:** database/performance; no table/column changes.

### Navigation, resilience and accessibility

**Before:** flat navigation, no deep-link/history behavior, vague fetch failures, weak modal keyboard behavior. **After:** grouped navigation, hash routes, owner-aware UI, clear network/HTTP errors, modal semantics/focus, Escape handling, visible focus styles, ARIA labels/live toasts and reduced sensitive dead-end actions. **Why:** faster wayfinding and safer operation. **Impact:** frontend only.

### Rate limiter bound

**Before:** source-IP buckets could accumulate indefinitely. **After:** stale buckets are pruned after a safe high-water mark. **Why:** prevents avoidable process memory growth. **Impact:** backend/security/performance; no API change.

## 8. Files changed

- `web_admin/app.py`
- `web_admin/templates/index.html`
- `web_admin/static/js/app.js`
- `web_admin/static/css/style.css`
- `database/db.py`
- `requirements.txt`
- `WEB_ADMIN_AUDIT_AND_OPTIMIZATION.md` (this report)

## 9. Database changes

No table or column was changed. Nine `CREATE INDEX IF NOT EXISTS` statements were added. They are backward compatible for SQLite and translated by the existing PostgreSQL compatibility layer.

## 10. API changes

- Sensitive endpoints now return HTTP 403 with `{"ok": false, "error": "Owner permission required"}` for non-owner sessions.
- `/api/security/2fa/setup` now returns a local `qr` data URI and no longer exposes the `otpauth` URI to browser code.
- Existing successful payloads for dashboard, analytics, profit and restock are unchanged.

## 11. Expected performance improvement

- Dashboard seven-day series: 7 aggregate queries → 1.
- Analytics trend/user series: about 56 daily queries → 4 grouped/count queries.
- Low-stock analytics: 1 product query + N stock queries → 1 grouped join.
- Profit: roughly 2N+1 data queries → 3 total data queries.
- Restock: 1+N queries → 1 grouped join.

Actual latency depends on row counts and database location; production query timing should be captured before/after deployment.

## 12. Testing and results

- Full Python `compileall`: passed.
- JavaScript syntax check with Node: passed.
- Project `verify_patch.py`: passed all checked files.
- Static route/security checks: passed; 79 Flask routes found, 16 sensitive routes owner-protected, external QR-service references removed.
- Original repository had no automated tests.
- Full Flask runtime/API smoke testing could not be executed in the sandbox because Flask and project dependencies were not installed and network access was unavailable. This limitation is explicit; runtime success is not claimed.
- Required next tests: credential + TOTP login, role 403 matrix, session revocation/idle timeout, product/stock CRUD, order refund/resend, deposit verification idempotency, encrypted stock delivery, broadcast retry, backup restore, SQLite and PostgreSQL integration, and responsive browser flows.

## 13. Remaining recommendations

Do not combine the remaining work into a rewrite. First add integration tests, then extract one domain at a time. Prioritize production configuration enforcement, consistent server validation, transaction hardening for financial/stock operations, server pagination, structured error logging and SQLite/Postgres CI. Run a staging deployment with a restored production-sized backup and capture endpoint timings before enabling changes in production.
