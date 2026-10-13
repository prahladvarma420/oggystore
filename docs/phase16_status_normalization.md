# Prompt 16.5 — explicit empty-delivery maintenance

## Why this is needed

Legacy order creation encrypted empty delivery text, producing non-empty
Fernet ciphertext. An authenticated decrypt can distinguish empty delivery
from a real short code; SQL cannot. Token length is not evidence of emptiness.

The shared API status resolver authenticates encrypted delivery before checking
presence. It works before cleanup: encrypted-empty delivery is processing.
SQL presence filters cannot be truthful until all historical reseller delivery
has been checked. Therefore `status=processing` **and** `status=completed`
return HTTP 409 / `delivery_normalization_required` until a full successful
explicit apply run records readiness in the existing settings table. Failed
and cancelled filters, unfiltered lists, detail, create and CSV do not require
this readiness flag. Unreadable non-terminal delivery produces sanitized 503 /
`order_state_unavailable`, never a guessed status.

There is no automatic cleanup at startup, deployment, on reads or on requests.
No table, column, index, terminal state or separate delivery store is added.

## Preconditions

1. Back up the database and test the procedure on a restored staging copy.
2. Deploy Prompt 16.5 to **every** bot, WebAdmin and API instance. Stop legacy
   writers before maintenance. Keep external/direct database writers stopped;
   they are not covered by the application invariant.
3. Use the existing database and its correct `DATA_ENCRYPTION_KEY` in the
   environment or repository `.env`. Do not paste secrets into commands or
   reports. Never run with an arbitrary replacement key.
4. Run from the repository root using the same Python environment/dependencies
   as the application. No app startup or migration is required by this tool.

Future empty reseller delivery is stored as NULL by order creation and the
shared safe writer, including authenticated encrypted-empty input. Existing
startup plaintext encryption skips raw empty reseller delivery, while customer
behavior remains unchanged. Real content (including whitespace) stays intact.

## SQLite dry-run — default, read-only

Replace the illustrative database path with the actual existing path:

```bash
python tools/normalize_reseller_empty_delivery.py \
  --db-path /absolute/path/to/shopbot.db --batch-size 100 --max-rows 5000
```

The SQLite connection is opened in read-only mode. The CLI deliberately avoids
importing `database.db`, whose historical imports can run migrations. Dry-run
changes no delivery, settings, ledger, wallet, schema or migration data. It
reports metadata counts only, never ciphertext, delivery text or keys.

A dry-run can preview at most 50,000 reseller rows per invocation; it does not
persist a new checkpoint or enable filters. Exit 3 means the bounded preview
is incomplete. Do not interpret a first-page preview as a whole-table audit.

## Explicit apply — only after reviewing the dry-run

```bash
python tools/normalize_reseller_empty_delivery.py \
  --db-path /absolute/path/to/shopbot.db \
  --apply --confirm NORMALIZE_EMPTY_RESELLER_DELIVERY \
  --batch-size 100 --max-rows 5000
```

Each invocation examines at most `max-rows` reseller rows, in batches of at
most `batch-size`. Allowed bounds: batch 1–500, invocation 1–50,000 rows.
Each batch is atomic and conditionally changes only the exact ciphertext
successfully decrypted to **the empty string**. A concurrent content change
is preserved and counted as a race, not overwritten. Customer rows are not
selected. Master labels, timestamps and all financial rows are untouched.

If exit 3 / `needs_resume: true`, manually repeat with `--resume`:

```bash
python tools/normalize_reseller_empty_delivery.py \
  --db-path /absolute/path/to/shopbot.db \
  --apply --confirm NORMALIZE_EMPTY_RESELLER_DELIVERY --resume \
  --batch-size 100 --max-rows 5000
```

The existing settings table records the bounded cursor and cutoff. Initial
apply invalidates prior readiness. Resume reuses that applied checkpoint;
readiness is enabled only after the full cutoff is checked with no unreadable
ciphertext and no races. New rows use the updated NULL-writing invariant.
Do not run concurrent cleanup jobs. Resuming a run whose readiness is already
true is rejected; a fresh applied run is the way to re-audit later.

## PostgreSQL

Use the already configured PostgreSQL `DATABASE_URL` and encryption key; omit
`--db-path`. The same flags and resume steps apply:

```bash
python tools/normalize_reseller_empty_delivery.py --batch-size 100 --max-rows 5000
python tools/normalize_reseller_empty_delivery.py \
  --apply --confirm NORMALIZE_EMPTY_RESELLER_DELIVERY --batch-size 100 --max-rows 5000
```

Dry-run sets PostgreSQL default transaction read-only. Apply uses existing
`pgcompat.atomic` boundaries. PostgreSQL execution must be validated in staging;
local verification for this release used temporary SQLite, not production PG.

## Exit codes / recovery

- **0:** bounded invocation finished. Filters are ready only when JSON says
  `ready: true` (only a successful complete applied run can say this).
- **1:** configuration/database failure; sanitized message, inspect environment.
- **2:** unreadable ciphertext/race, or CLI usage error. No guessed normalization.
- **3:** more rows remain; applied runs can be resumed manually.

Unreadable ciphertext is never cleared. Check the correct original encryption
key and resolve the issue without deleting real content. After resolving a
key problem or race, run a **fresh** explicit apply audit (without `--resume`)
to reset accumulated failure counters. A failed readiness audit leaves SQL
presence filters closed, even when earlier verified batches were committed.

## Financial and analytics expectations

Wallet, ledger, refund, statement money totals, gross/net sales, quantities sold
and total order counts are unchanged. Processing orders still count as sales;
no analytics query or persisted financial model was changed.

Correcting encrypted-empty to NULL can move historical orders from the
completed bucket to pending, and can reduce the **completed-only** sales
classification. This is a correction of the prior misleading delivery
classification, not a refund, loss of gross/net sales or money movement.
Do not assert that every analytics classification remains identical.

## Acceptance probes after apply

Verify JSON readiness, then call authenticated `GET /api/v1/orders?status=processing`
and `?status=completed`; each returned row must have the corresponding status.
For a pending manual order, use the existing owner Mark delivered action.
Confirm list/detail/CSV move to completed and the real
`GET /api/v1/orders/<id>/delivery` returns expected items. Check reconciliation
and financial totals against the pre-maintenance baseline. Do not include
actual delivery content in logs, reports or webhook payloads.

If legacy or direct writers are reintroduced, stop using the readiness
certificate and re-audit with all writers updated. SQL cannot independently
validate arbitrary ciphertext or recover a lost encryption key. Do not turn
on readiness by editing settings manually or infer content from token size.
