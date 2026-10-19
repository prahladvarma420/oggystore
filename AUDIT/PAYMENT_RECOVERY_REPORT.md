# OggoStore emergency payment recovery — final report

## Decision and source binding

GitHub `main` **0d25bac6c2255ed636c6db848314f3d65e82b0ce**, re-fetched before final full run; local and fetched HEAD agree. No GitHub write/commit or production deployment performed. **BEP20: fixed and locally tested. Binance: blocked for live access**, despite passing local accounting/failure tests. Overall **NOT production-ready / not live verified**. No real funds, customer records, wallet balances or production configuration changed.

## Confirmed causes and limits of attribution
1. **BEP20 checkpoint loss** — `_get_recent_incoming_usdt_logs` clamped an existing checkpoint to the rolling lookback. After a long outage this skipped still-unprocessed blocks. Regression reproduced before fixing. Existing checkpoints now resume at last + 1 in bounded chunks; fresh scans retain the old bounded lookback. Already skipped historical intervals need reviewed reconciliation, not an automatic balance credit.
2. **BEP20 ineffective failover** — `_drop_cached_web3` discarded a failed connection but `_get_web3` always retried the first URL. A responsive primary that rejected logs could monopolize retries. Store/rotate endpoint position; preserve chain ID validation and bounded provider timeouts. Before/after regression verifies actual alternate choice.
3. **BEP20 missing payment proof / unsafe automatic fallback** — the original log path did not independently compare successful receipt and canonical block, and a balance increase could generate a pseudo-transaction credit. A balance delta does not prove payer, transfer, invoice or execution. Tests demonstrated invalid-receipt credit and balance-only credit before hardening. Require exact successful canonical receipt/log evidence before settlement; keep balanceOf as diagnostics/scan trigger only. Provider failures hold checkpoint/credit for retry. The internal legacy fallback helper remains for compatibility with existing trusted-call tests; no scheduled payment checker invokes it. This is an intentional safety change, not restored unsafe behavior.
4. **Feature retirement dependency** — old invoice claim code unconditionally queried the experimental claims table. Removing schema startup without changing this would break clean deployments, or deleting claims would permit replay. Optional metadata-checked, read-only historical claims/accounted-event checks preserve replay protection and work on a fresh database. No unique address service/schema initialization remains.
5. **Binance upstream HTTP 451** — supplied log contains 14 records / five 451 mentions. This occurs in signed Pay trade-history fetching before wallet matching/settlement. Actual region/account/gateway restriction cause cannot be inferred without provider access. Latest GitHub already has sanitized error categories, retry cooldown, response validation and signing repair; `payments/binance_checker.py` is deliberately unchanged. Ten failure/recovery tests pass; private native settlement tests pass. Do not call this live fixed. Owner must verify approved account, permission/IP restrictions and permitted deployment access with Binance. No restriction bypass added.
6. **Missing shared docs** — latest uploads omitted six shared API/operations/backlog/versioning/audit/checklist contracts required by existing cumulative guards. Restore previously delivered shared documents with historical notices, not the feature-only documents. This is necessary contract restoration, not evidence of a live rehearsal.

**Attribution:** verified code defects are not proof that Phase16–17 caused every reported production failure. No production database/provider trace was available. Git history establishes exactly which unique-address hunks were added, and confirms newer Binance diagnostics must be preserved.

## History and baseline verification
- Pre-feature parent `aa133a5`, feature commits `7f19f7a` (two bootstrap lines), `abbefce` (49 UI lines), `d24af75` (core/schema/claim dependency), later `93144d5` / `ce149a9` tests/process. Removed only attributable feature code, not all later commits.
- `database/reseller_db.py`, `handlers/reseller_handlers.py`, `Procfile` now match the recovered pre-feature versions byte-for-byte. The independent deterministic collision fixture in `tests/test_phase17_topup_flow.py` stays unchanged from current HEAD.
- Reviewed implementation/plan/report documents from the supplied earlier patch. They are already absent from latest tracked HEAD, so they are not recreated. Seven tracked feature-only files have explicit deletions; no other existing tests deleted.
- Compared supplied `oggystore-main (2).zip` integration points with current HEAD. Database handler/settlement/checker/Procfile/customer-test baseline matches; Binance and unique core differ in newer HEAD. Preserved newer Binance code rather than restoring that older archive. Original archive hash and file comparisons are in results JSON.
- Earlier customer deposit and unrelated backup/analytics fixes remain. `database/db.py`, `database/pgcompat.py`, `utils/backup.py`, `web_admin/app.py`, `payments/deposit_handlers.py`, `payments/reseller_deposits.py`, `payments/binance_checker.py`, `bot.py`, `config.py`, `tests/test_phase12_final_regression.py`, `tests/test_phase17_topup_flow.py` remain byte-for-byte current HEAD.

## Final commands and actual results
Commands shown as `python` used `/data/recovery-venv/bin/python`, Python 3.13, from the repository root. Dependencies were installed from `requirements.txt` plus PyYAML; examples used locked `npm ci`; local Chromium and PostgreSQL 15 tools were available. External provider/Telegram boundaries were mocked in ordinary payment tests. SQLite/native PostgreSQL transactions are real.

| Command | Actual result |
|---|---|
| `git fetch origin main --quiet` / `git rev-parse HEAD origin/main` | Both bind the source revision above; no remote changes. |
| `python tools/run_tests.py` (final) | 108 isolated suites; **2,225 cases: 2,215 passed, 10 skipped, 0 failures, 0 errors; exit 0**. 105 PASS, one fully SKIPPED, two PASS_WITH_NOTES. |
| `python tools/run_deposit_postgres.py` (final) | **8/8 passed, 0 skipped; exit 0** on a new private PostgreSQL 15 cluster (0.959s test runtime); no production binding. |
| `python tools/run_tests.py --filter deposit_restore` | 13/13 passed; exit 0. |
| `python tools/run_tests.py --filter deposit` | 77 cases: 68 passed, 9 disclosed skips; exit 0. |
| `python tools/run_tests.py --filter binance` | 10/10 passed; exit 0. |
| `python tools/run_tests.py --filter git_history_scan` | 3/3 passed; exit 0, temporary Git repos only. |
| `python -m compileall -q .` | Passed; exit 0. |
| `python verify_patch.py` | Established markers passed; exit 0. Its historical success prose is not a production certification. |
| `python tools/generate_client_kit.py --check` | Five generated artifacts consistent; exit 0. |
| `git diff --check` | Passed; exit 0. |
| Fresh Git archive + delivered ZIP + explicit deletions | Full intended source bytes matched; repeated 13 recovery and 10 Binance tests passed. No missing payload files. |
| `node --check web_admin/static/js/app.js` and receiver.js | Passed; exit 0. Initial command used nonexistent static/app.js and failed; corrected rather than calling that failure a pass. |
| `python tools/scan_secrets.py` | Current text tree: zero findings/read errors; final report/manifest rescan recorded in results. Pattern scan only. |
| `python tools/scan_git_history.py` | **Exit 1: four findings across two historical session files**, 1,032 text blobs / 12 binary/non-UTF8 objects. This is an unresolved exposure, not a green scan. |

All per-file suite counts/runtimes are in `AUDIT/PAYMENT_RECOVERY_RESULTS.json`. The earlier first complete run had 2,210 passes + 10 skips; final adds two payment cases and three scanner tests, not a disappearing failure. Pre-fix focused failures (checkpoint, rotation, old feature routing; later six receipt/balance failures including subtests) are recorded and retained as failure evidence, not misreported successes.

### Skips, cumulative guards and UI boundaries
- Eight new native cases intentionally skip under the ordinary runner; all separately executed and passed under the private harness. Existing Phase10 optional `webadmin` directory guard skips once; existing Phase17 unconditional native placeholder skips once. Neither assertion was weakened or renamed as a pass.
- Full run includes unchanged customer Phase12 guard, P12/P13/P14/P15 security audits, Phase15 financial suites, Phase16/17 capstones, reseller replay/ledger/reconciliation, webhook/signature/JS receiver round trips, docs/OpenAPI parity and test-runner self-check.
- Existing local Chromium/Flask tests ran: six whole-project browser cases (20 WebAdmin views at desktop/mobile sizes and targeted controls), plus twelve reseller-tab tests (read services and browser navigation/retry). Real local rendering is not live owner login/2FA, Railway UI, real Telegram delivery or external payment verification.
- Mechanical every-tracked-file inventory/hash/AST/config parse results are separate. It is not a claim of exhaustive manual line-by-line review or zero remaining defects. No coverage percentage, hosted CI run, actual Binance account test, real chain payment, production affected-record query or staging load run was performed.

## Security findings — action required
**HIGH: publicly committed Microsoft browser-session exports** at historical `m365_session.json` and `tools/m365_session.json`, present in fetched Git history despite current-tree deletion (including historical commits `b08fab8`/`274a2ec`; deletion `a2d7b4c`). Nonempty Microsoft/login cookies were verified structurally; their current validity was NOT tested. Revoke affected sessions/review account activity immediately, rotate associated credentials as required. No session value is reproduced or used. Private GitHub visibility is not revocation and does not erase forks/caches/history. No history rewrite performed or authorized. Scan scope excludes binary/LFS/unfetched refs and cannot detect every arbitrary secret/seed.

## Financial and deployment boundaries
No wallet schema/type change, no zero-value ledger entries, no direct balance patch, no destructive SQL, no expiry/tolerance reduction, no unique custody provider, no new API. Ordinary customers and resellers retain their separate deposit records/accounting. Atomic shared claim and financial/audit rollback tests cover duplicate/concurrent/cross-wallet processing on SQLite and native PostgreSQL. The 30-second existing bot scheduling is unchanged.
`docs/PAYMENT_RECOVERY.md` is the required deployment/rollback/affected-record procedure. Stop/remove the separately configured retired Railway service; deploy all shared writers consistently; preserve database bindings/encryption keys. Existing five retired tables are **not dropped**. Optional historical reservation checks remain solely to prevent replay. Production contents, issued-address inventory and retained fractions are unknown; inspect read-only and involve the custody/account owner if any exist. No blind checkpoint reset or historical credit.

## File-by-file change audit
Paths below are repository-relative. ZIP contains added/modified files only, no outer project folder. Deletions must be applied explicitly using the supplied instructions. Original HEAD and final SHA-256 values are in the manifest; deleted contents are not packaged. The manifest itself is covered by the external ZIP checksum rather than a self-referential hash.

| Action | Path | Purpose |
|---|---|---|
| modified | `.github/workflows/tests.yml` | Replace the retired feature-only native gate with the private invoice PostgreSQL gate; preserve all other CI steps. Hosted execution not claimed. |
| added | `AUDIT/PAYMENT_RECOVERY_FILE_REVIEW.json` | Mechanical tracked-file inventory, syntax/config classifications and hashes; explicitly not manual proof of every behavior. |
| added | `AUDIT/PAYMENT_RECOVERY_PLAN.md` | History-bound restoration plan and scope. |
| added | `AUDIT/PAYMENT_RECOVERY_REPORT.md` | Current evidence-bound root causes, file-by-file audit, commands, results and limitations. |
| added | `AUDIT/PAYMENT_RECOVERY_RESULTS.json` | Actual per-suite counts, runtimes and static/security outcomes, not hypothetical results. |
| deleted | `AUDIT/RESELLER_BEP20_PHASE2_RESULTS.json` | Abandoned unique-address feature only; remove explicitly; no production table or balance is deleted. |
| added | `GO_LIVE.md` | Restore omitted shared go-live checklist; historical evidence remains explicitly historical, NOT READY gates preserved. |
| added | `OPERATIONS.md` | Restore omitted shared application operations runbook, required by existing cumulative guards. |
| added | `PHASE16_OPERATIONS_AUDIT.md` | Restore omitted historical Phase16 audit with an explicit notice, not new production certification. |
| modified | `Procfile` | Remove only reseller-deposit-worker; restore the four pre-feature commands byte-for-byte. |
| modified | `RAILWAY_RESELLER_API.md` | Restore missing deployment/runbook content from the prior delivered shared guide, including API and both operational workers; no unique-address service. |
| added | `RESELLER_API.md` | Restore omitted shared authoritative API guide/error catalog from prior patch; existing client-kit and route parity checks pass. |
| added | `RESELLER_API_FUTURE_PROMPTS.md` | Restore omitted master backlog without closing unverified live gates. |
| added | `RESELLER_API_VERSIONING.md` | Restore omitted versioning contract used by cumulative regressions. |
| modified | `database/reseller_db.py` | Remove only the two unique-schema bootstrap lines; existing reseller invoice tables, ledger, analytics and backup fixes preserved. |
| added | `docs/PAYMENT_RECOVERY.md` | Exact source-bound deployment, explicit deletion commands, backup, retained tables, verified-payment reconciliation, Binance blocker and rollback. |
| added | `docs/payment-checkers.env.example` | Safe placeholders and existing variable names only; no deployed secret replacement. |
| modified | `handlers/reseller_handlers.py` | Remove only 49 feature-specific address/QR/history lines; restore invoice BEP20 selection and all other Reseller Center features. |
| modified | `payments/bep20_checker.py` | Repair checkpoint catch-up and RPC rotation; require successful receipt/canonical exact Transfer log proof; remove automatic balance-only pseudo-payment credits; retain old invoice matching/expiry/accounting. |
| modified | `payments/deposit_settlement.py` | Replace mandatory experimental table dependency with optional read-only old reservation/accounted-event replay guard; preserve shared lock, cross-wallet claims and atomic accounting. |
| deleted | `payments/unique_bep20.py` | Abandoned unique-address feature only; remove explicitly; no production table or balance is deleted. |
| deleted | `payments/unique_bep20_schema.py` | Abandoned unique-address feature only; remove explicitly; no production table or balance is deleted. |
| deleted | `reseller_api/workers/reseller_deposit_worker.py` | Abandoned unique-address feature only; remove explicitly; no production table or balance is deleted. |
| modified | `tests/test_customer_deposit_regression.py` | Enrich mock RPC evidence with receipt, canonical block hash and log index only; all 27 original assertions/cases retained. |
| added | `tests/test_deposit_postgres.py` | 8 native PostgreSQL integration tests gated to a test-owned local disposable cluster; concurrency, claims, rollback and clean bootstrap. |
| added | `tests/test_deposit_restore.py` | 13 new focused real-SQLite/checker tests with external RPC/Telegram mocked; catches old feature routing, scanner defects, receipt failures and historical replays. |
| added | `tests/test_git_history_scan.py` | 3 isolated temporary-Git tests for history scanner detection/redaction/error exit. |
| modified | `tests/test_phase17_reseller_deposits.py` | Enrich mock RPC evidence only; existing financial/security assertions unchanged, including preexisting skip. |
| modified | `tests/test_reseller_api_infrastructure.py` | Restore exact four-process Procfile expectation; do not remove original process assertions. |
| deleted | `tests/test_unique_bep20_postgres.py` | Abandoned unique-address feature only; remove explicitly; no production table or balance is deleted. |
| deleted | `tests/test_unique_reseller_bep20.py` | Abandoned unique-address feature only; remove explicitly; no production table or balance is deleted. |
| added | `tools/run_deposit_postgres.py` | Private non-root local PostgreSQL harness, sanitized environment, no TCP/production DB reuse; replaces feature-only harness. |
| deleted | `tools/run_unique_bep20_postgres.py` | Abandoned unique-address feature only; remove explicitly; no production table or balance is deleted. |
| added | `tools/scan_git_history.py` | Read-only metadata-only scan of reachable fetched Git blobs; never prints secret values or rewrites history. |
| added | `AUDIT/PAYMENT_RECOVERY_MANIFEST.json` | Explicit path/action/original/final SHA-256 manifest and deletion instructions; self-hash excluded. |

## Completion status
Local restoration and executable repair work is complete within the available environment. **Do not declare production deposits restored yet.** Required owner actions: revoke exposed sessions; verify Binance upstream access; back up and inspect retired/affected data; stop retired worker; merge/deploy the reviewed patch; perform authorized real/staging payment and retry/reconciliation checks. No production-ready or live-verified claim is made.
