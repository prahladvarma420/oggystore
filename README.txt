Oggy Telegram Shop Bot - Complete Fixed Build
Included fixes:
- Fixed Smart Dashboard missing function
- Added/verified Full Admin Logs
- Added/verified Create Backup and Restore Backup
- Fixed Admin Panel callback compatibility
- Kept colorful buttons and existing features
- Cleaned README/documentation files into this single README

Run:
python bot.py

Admin features:
- Smart Dashboard
- Products & Stock
- Orders & Users
- Marketing & Growth
- Security & System
- Support & Admins
- Message Templates
- Coupons
- Auto Marketing
- Admin Logs
- Create/Restore Backup

==============================================================================
Reseller Platform & Financial Suite (Phases 1-15)
==============================================================================

Reseller API (separate Railway service, same database):
- API keys (rk_test_/rk_live_), product import & pricing, quotes, atomic
  wallet-funded orders with idempotency, delivery retrieval, signed webhooks
  (HMAC + freshness window + retries/DLQ), rate limits & quotas, sandbox
  environment with fail-closed isolation.
- Developer docs: RESELLER_API.md + docs/openapi.yaml + versioning policy in
  RESELLER_API_VERSIONING.md.

Financial suite:
- Wallet + immutable transaction ledger (USDT integer cents; triggers block
  update/delete on both SQLite and PostgreSQL).
- Invoices (draft/finalized/void; INV-YYYY-NNNNNN; INV-XXXXXXXXXXXX public
  ids; printable HTML documents) and statements (open/finalized;
  ST-YYYY-NNNNNN; STMT-* public ids; half-open shop-time periods).
- Refund records + adjustment records with an owner approval workflow
  (create -> approve -> complete / reject / cancel), every money movement
  atomic with its business record.
- Read-only reconciliation (runs + discrepancies + resolution workflow;
  detection only, never auto-repairs money).
- WebAdmin: financial overview, ledger browser + full trace, unified
  financial search, invoices/statements/refunds/adjustments management,
  reconciliation console.
- Reseller API: /transactions, /refunds, /invoices(+lines/transactions/
  document), /statements(+transactions/invoices/document), /wallet/summary,
  /analytics/sales, /usage, /exports/orders.csv.

Run the full test battery: each suite runs standalone
(python3 -m unittest tests.<name> -q).
==============================================================================
Running the tests
==============================================================================
Use Python 3.13 with dependencies installed (no production credentials needed).
For the Phase 17.7 JavaScript receiver/schema tests, install Node.js 24+ and run:
  npm ci --prefix examples --ignore-scripts
Without Node 24+, those checks are explicit skips, not verified JavaScript passes.
With Node present, missing example dependencies are test failures, not skips.

Python commands:
  python -m pip install -r requirements.txt pyyaml
  python tools/run_tests.py
  python tools/run_tests.py --filter phase15
  python tools/run_tests.py --filter phase17_test_runner --timeout 300

The runner resolves the repository relative to itself. An absolute path to
/path/to/repo/tools/run_tests.py also works from outside the repository.
Each tests/*.py file (except __init__.py) runs in its own Python subprocess,
using unittest's normal module loader and runner. Non-test-named HTTP helper
suites run too; wrappers also import some of their cases, so totals are executed
cases, not a count of distinct test methods. Do NOT use shared-process unittest
discovery: import-time temporary database setup makes it unsafe for this suite.

Each suite gets a disposable source copy, without .env, login sessions, runtime
databases, logs, backups, dependencies or Git metadata. Its environment keeps
only platform/locale/temp/PATH/PYTHONPATH settings and synthetic test config;
production credentials and DATABASE_URL are not inherited. Dependencies must be
installed in the selected interpreter (a local PYTHONPATH is also supported).
Nested commands using literal python are kept on that interpreter via PATH.
The qrcode[pil] requirement includes Pillow for the existing QR image tests.
Test-created default databases therefore cannot touch the working checkout.
No live PostgreSQL/Telegram/provider claims are made by this SQLite test run.

The default timeout is 300 seconds per suite; --timeout accepts positive seconds.
Full merged stdout/stderr, per-suite structured counts and summary.json are saved
under .test-results/<unique-run>/. Use --output-dir /outside/repo/path to override.
Failures print an output tail; full tracebacks remain in the log. A failed,
crashed, empty, missing-result or timed-out suite fails the run, without stopping
later suites. On POSIX, timeouts kill the whole child process group; on Windows,
only the direct child is guaranteed to be terminated. Partial skips/expected
failures are disclosed as PASS_WITH_NOTES, all-skipped suites as SKIPPED, never
as passed cases. Exit 0 means successful assertions with no suite failures;
exit 1 means failure, 2 invalid/no-match/all-skipped, 130 interruption. A mixed
run may exit 0 with explicit skips; it does not mean every external probe ran.

Local CI-equivalent static checks:
  python -m compileall .
  python verify_patch.py
  python tools/scan_secrets.py
The offline scanner includes tests/docs and flags provider-token, private-key,
credential-URL and high-entropy literal credential-assignment patterns. It never
prints matched values. Runtime/dependency/binary artifacts are excluded; generic
obvious whole-value placeholders are allowed, but provider-token signatures are
not exempted. This bounded pattern check cannot prove that no arbitrary secret
exists; keep real credentials out of source control. GitHub Actions runs these
checks and uploads test artifacts even after failures. Local checks do not prove
that the hosted GitHub workflow has run or passed.

Client kit (Phase 17.7)
----------------------
Working Flask/Express webhook receivers, blank Postman test/production
environments, raw-body signature requirements and durable inbox/retry guidance
are in examples/README.md. Generate/check the OpenAPI collection and code-derived
error catalog with:
  python tools/generate_client_kit.py
  python tools/generate_client_kit.py --check
  python tools/run_tests.py --filter phase17_client_kit
Never commit filled credentials, signing secrets or receiver SQLite files.

Manual staging performance validation (Phase 17.8)
------------------------------------------------
TEST-only Railway/PostgreSQL procedure: docs/staging_load_validation.md
Tools: tools/seed_staging_load.py, tools/load_test.py,
       tools/verify_staging_load.py
No default load execution; private configuration and explicit --apply/--execute
are required. Never use production. Actual results/blockers are recorded in
PHASE17_LOAD_VALIDATION.md; unit tests do not prove staging performance.
