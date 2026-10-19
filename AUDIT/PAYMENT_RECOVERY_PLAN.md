# Deposit recovery plan — inspected GitHub main 0d25bac6c2255ed636c6db848314f3d65e82b0ce

Fetched main and Git history directly. Unique feature introduction is isolated in
7f19f7a (schema registration), abbefce (Telegram UI), d24af75 (core/schema/claim),
later worker/tests/Procfile uploads, and Phase2 hardening. Pre-feature parent of
7f19f7a is aa133a5: its earlier customer-deposit regression repair must survive.
Do not bulk-checkout that tree: newer Binance diagnostics and unrelated fixes stay.

Remove unique allocation/core/schema, dedicated worker/process, exclusive tests,
native harness and obsolete results. Restore handlers/database startup with exact
history hunks; retain a read-only optional historical-payment reservation check so
existing event credits cannot replay as ordinary invoices. Do NOT create or drop
retired tables; production contents unknown. No balance/backfill operation.

BEP20 scanner has two reproducible recovery defects: unconditional lookback clamp
jumps over an existing unprocessed checkpoint, and dropping a failing RPC retries
the primary rather than rotating. Reproduce with tests first, then repair minimally.
Also verify actual receipt/canonical evidence and safe retry before checkpoint.
Binance log451 is an upstream access failure, not proof of wallet-routing failure;
retain sanitized cooldown/signing and do not bypass restrictions. Provider account,
Railway environment and production affected payments are not accessible.

Restore six missing shared documentation contracts and the current Railway guide
from prior delivered patch only after comparing with current routes. Earlier unique
feature docs are review evidence, not reinstated abandoned functionality.

Run isolated full regression, focused invoice tests, native disposable PostgreSQL,
compile/marker/client-kit checks, source and full Git-blob secret-pattern scan.
Deliver only intended changes plus explicit deletions and original/final hashes.
