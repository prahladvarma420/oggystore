"""Atomic reseller order creation — Phase 7 Prompt 5.

Transaction architecture
------------------------
The master stock system commits stock reservations immediately (one commit per
stock row, per FIX43/FIX44).  The reseller wallet uses pgcompat.atomic() which
on SQLite executes BEGIN IMMEDIATE + COMMIT and on Postgres uses a real
transaction. These two subsystems each manage their own boundary.

The reseller order atomic unit wraps wallet debit + order INSERT together:

    stock reserved (committed separately; stock_ids tracked for rollback)
        │
        ▼
    pgcompat.atomic(conn)          ← BEGIN IMMEDIATE
        ├─ post_wallet_transaction  ← SAVEPOINT (nested atomic inside outer BEGIN)
        │   ├─ UPDATE reseller_wallet    (balance deduct)
        │   └─ INSERT wallet ledger      (immutable ledger row)
        └─ INSERT INTO orders            (also inside outer BEGIN IMMEDIATE)
        │
        COMMIT  (wallet balance + ledger + order all commit together)

On any exception inside the atomic block → ROLLBACK (wallet + order)
Then immediately → stock.release_stock() to undo the stock reservation

This ensures:
  - Wallet is never debited without a committed order, and vice versa.
  - Stock is released whenever wallet or order fails.
  - No partial financial state can persist.

Idempotency
-----------
Wallet: post_wallet_transaction() enforces UNIQUE(reseller_id, tx_type,
        reference) and returns ('duplicate', txid) on exact repeat.
Order:  UNIQUE INDEX idx_orders_reseller_idempotency ON
        orders(reseller_id, client_order_id) WHERE client_order_id IS NOT NULL.

Client-facing idempotency (client_order_id):
  1. Before any DB writes, check if an order already exists for
     (reseller_id, client_order_id).  If yes, return the existing order
     immediately and release any freshly-reserved stock.
  2. If the concurrent INSERT races and hits the unique constraint, catch it,
     fetch the existing order, and return it.

Wallet reference = "rapi.{reseller_id}.{request_id}"  (always server-generated;
  the client_order_id may contain unsafe chars and is NOT used as the reference).
  On true retry (same client_order_id, new request_id), the early lookup in step
  1 returns the existing order before the wallet is touched — no double debit.
"""

import database.pgcompat as _pgcompat
from database.db import (
    _safe_add_column,
    get_conn,
    get_cursor,
)
from database.reseller_db import (
    post_wallet_transaction,
    from_cents,
)
from utils.crypto import encrypt_text
from utils.timezone import now_text

from reseller_api.services.order_validation import ValidatedOrderRequest
from reseller_api.services.order_pricing import OrderPricingResult
from reseller_api.services.order_stock import StockReservationResult
from reseller_api.context import ApiRequestContext


# ---------------------------------------------------------------------------
# Inline delivery formatter
# (product_handlers.py imports `telegram` at module level so we cannot import
# from it in a plain Python context; copy the pure logic here instead.)
# ---------------------------------------------------------------------------

def _format_delivery_items(items):
    """Join a list of delivery strings for storage in the orders table."""
    if isinstance(items, list):
        items = [str(i) for i in items]
        if len(items) > 1 and any("\n" in it for it in items):
            sep = "\u2500" * 28
            out = []
            for idx, it in enumerate(items, 1):
                out.append(
                    f"{sep}\n\U0001f464 ACCOUNT {idx} of {len(items)}\n{sep}\n{it}"
                )
            return "\n\n".join(out)
        return "\n\n".join(items)
    return str(items)


# ---------------------------------------------------------------------------
# Schema additions (idempotent; called once at app start)
# ---------------------------------------------------------------------------

def ensure_order_metadata_columns():
    """Add reseller API metadata columns to the master orders table.

    Called from ensure_reseller_tables() so it runs exactly once at startup.
    All operations are idempotent (_safe_add_column silently skips existing cols).
    """
    conn = get_conn()
    cur  = get_cursor()
    for col, defn in [
        ("source",            "TEXT"),    # 'reseller_api' for all reseller orders
        ("reseller_id",       "INTEGER"), # resellers.id (distinct from user_id)
        ("reseller_key_id",   "TEXT"),    # api_key_public_id — never the secret
        ("client_order_id",   "TEXT"),    # client-supplied idempotency key
        ("request_id",        "TEXT"),    # server-generated request_id
        ("quantity",          "INTEGER"), # ordered quantity (units)
        ("unit_price_cents",  "INTEGER"), # server-side unit price in cents
    ]:
        _safe_add_column("orders", col, defn)
    # Partial unique index enforces client_order_id idempotency per reseller.
    # WHERE client_order_id IS NOT NULL avoids collisions with customer orders.
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_reseller_idempotency
        ON orders(reseller_id, client_order_id)
        WHERE client_order_id IS NOT NULL
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class OrderServiceError(ValueError):
    """Raised when order creation fails for a business reason."""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(code)
        self.code    = str(code)
        self.message = str(message)
        self.status  = int(status)


# ---------------------------------------------------------------------------
# Result value object
# ---------------------------------------------------------------------------

class OrderResult:
    """Immutable result of a successful (or idempotent duplicate) order."""

    __slots__ = (
        "order_id",
        "public_order_id",
        "wallet_transaction_id",
        "wallet_reference",
        "is_duplicate",
        "created_at",
    )

    def __init__(
        self,
        order_id: int,
        wallet_transaction_id: int,
        wallet_reference: str,
        is_duplicate: bool = False,
        created_at: str = "",
    ):
        self.order_id              = int(order_id)
        self.public_order_id       = f"ORD-{int(order_id):08d}"
        self.wallet_transaction_id = int(wallet_transaction_id)
        self.wallet_reference      = str(wallet_reference)
        self.is_duplicate          = bool(is_duplicate)
        self.created_at            = str(created_at)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_wallet_reference(context: ApiRequestContext) -> str:
    """Build a wallet-reference-regex-safe idempotency key.

    Format: "rapi.{reseller_id}.{request_id}"
    request_id is server-generated (UUID-like with alphanumerics and hyphens);
    both components satisfy [A-Za-z0-9:_.-]{0,127}.
    """
    return f"rapi.{context.reseller_id}.{context.request_id}"[:128]


def _build_delivery_text(
    validated: ValidatedOrderRequest,
    stock: StockReservationResult,
) -> str:
    """Return the delivery text to encrypt and store on the order row.

    Code products: join decrypted delivery strings from stock.delivery_items.
    Manual products: fetch the static delivery field from products table.
    """
    if stock.stock_type == "code" and stock.delivery_items:
        return _format_delivery_items(stock.delivery_items)
    cur = get_cursor()
    cur.execute("SELECT delivery FROM products WHERE id=?", (validated.product_id,))
    row = cur.fetchone()
    return (row[0] or "") if row else ""


def _get_existing_order_id(reseller_id: int, client_order_id: str):
    """Return order_id if (reseller_id, client_order_id) already exists."""
    if not client_order_id:
        return None
    cur = get_cursor()
    cur.execute(
        "SELECT id FROM orders WHERE reseller_id=? AND client_order_id=?",
        (reseller_id, client_order_id),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def _last_purchase_debit_id(reseller_id: int) -> int:
    """Most recent purchase_debit tx id for this reseller (used in dup returns)."""
    cur = get_cursor()
    cur.execute(
        "SELECT id FROM reseller_wallet_transactions "
        "WHERE reseller_id=? AND tx_type='purchase_debit' ORDER BY id DESC LIMIT 1",
        (reseller_id,),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def create_reseller_order(
    validated: ValidatedOrderRequest,
    pricing: OrderPricingResult,
    stock: StockReservationResult,
    context: ApiRequestContext,
) -> OrderResult:
    """Atomically create a reseller order and debit the wallet.

    Args:
        validated:  Validated order request (from order_validation).
        pricing:    Server-side pricing result (from order_pricing).
        stock:      Already-reserved stock (from order_stock).
                    Must NOT have been released before calling here.
        context:    Authenticated API request context.

    Returns:
        OrderResult on success or idempotent duplicate.

    Raises:
        OrderServiceError: On wallet or database failure.
                           stock.release_stock() is called automatically.

    Ownership contract:
        This function takes ownership of `stock`.  It will call
        stock.release_stock() on any exception.  On success the stock
        reservation is permanent (already committed by reserve_and_validate_stock).
    """
    conn = get_conn()
    cur  = get_cursor()

    # ------------------------------------------------------------------
    # 1. Client-order-id idempotency pre-check (before any DB write)
    #    Prevents double debit when the client retries with the same
    #    client_order_id on a different request_id.
    # ------------------------------------------------------------------
    if validated.client_order_id:
        existing_id = _get_existing_order_id(
            validated.reseller_id, validated.client_order_id
        )
        if existing_id is not None:
            # True duplicate: release the freshly-reserved stock (it's extra).
            stock.release_stock()
            return OrderResult(
                order_id=existing_id,
                wallet_transaction_id=_last_purchase_debit_id(validated.reseller_id),
                wallet_reference="(duplicate)",
                is_duplicate=True,
            )

    # ------------------------------------------------------------------
    # 2. Build order parameters (all from server-side values; never client)
    # ------------------------------------------------------------------
    wallet_reference = _make_wallet_reference(context)
    delivery_text    = _build_delivery_text(validated, stock)
    product_name_str = f"{validated.product_name} x{validated.quantity}"
    # orders.price is a REAL column (legacy schema); convert integer cents.
    price_real       = pricing.total_cents / 100.0
    wallet_desc = (
        f"Order: {validated.public_product_id} "
        f"x{validated.quantity} @ {pricing.unit_price_display} USDT"
    )

    # ------------------------------------------------------------------
    # 3. Atomic block: wallet debit (SAVEPOINT) + order INSERT
    #
    #    pgcompat.atomic(conn) on SQLite:
    #      - top-level  → BEGIN IMMEDIATE … COMMIT
    #      - nested      → SAVEPOINT … RELEASE
    #    post_wallet_transaction also calls pgcompat.atomic(conn) internally;
    #    since conn.in_transaction is True inside our outer block, it is
    #    promoted to a SAVEPOINT, making wallet balance + ledger row part of
    #    the same outer BEGIN IMMEDIATE that also contains the orders INSERT.
    #    The single outer COMMIT makes all three durable together.
    # ------------------------------------------------------------------
    wallet_tx_id = None
    order_id     = None
    try:
        with _pgcompat.atomic(conn):        # BEGIN IMMEDIATE

            # --- 3a. Wallet debit ---
            status, tx_data = post_wallet_transaction(
                reseller_id=validated.reseller_id,
                tx_type="purchase_debit",
                amount_cents=-pricing.total_cents,   # negative = debit
                reference=wallet_reference,
                description=wallet_desc,
                related_order_reference=wallet_reference,
            )

            if status == "insufficient_funds":
                raise OrderServiceError(
                    "insufficient_balance",
                    "Insufficient wallet balance for this order.",
                    402,
                )
            if status == "idempotency_conflict":
                raise OrderServiceError(
                    "idempotency_conflict",
                    "Wallet reference already used with a different payload.",
                    409,
                )
            if status not in ("posted", "duplicate"):
                raise OrderServiceError(
                    "wallet_error",
                    f"Wallet transaction failed: {status}",
                    500,
                )
            wallet_tx_id = int(tx_data)

            # --- 3b. Order INSERT ---
            cur.execute(
                """
                INSERT INTO orders(
                    user_id, product_name, price, delivery, product_id,
                    payment_method,
                    source, reseller_id, reseller_key_id,
                    client_order_id, request_id,
                    quantity, unit_price_cents
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    0,                              # user_id=0 for reseller orders
                    product_name_str,               # e.g. "Widget A x2"
                    price_real,                     # REAL column (legacy total)
                    encrypt_text(delivery_text),    # encrypted delivery
                    validated.product_id,
                    "reseller_api",                 # payment_method label
                    "reseller_api",                 # source tag
                    validated.reseller_id,          # resellers.id
                    context.api_key_public_id,      # public key ID, never secret
                    validated.client_order_id,      # client idempotency key
                    context.request_id,             # server request_id
                    validated.quantity,             # units ordered
                    pricing.unit_price_cents,       # server-side unit price
                ),
            )
            order_id = cur.lastrowid
            # Fetch the created timestamp inside the transaction (pre-commit)
            cur.execute("SELECT created FROM orders WHERE id=?", (order_id,))
            _ts_row  = cur.fetchone()
            _created = _ts_row[0] if _ts_row else ""
            # Outer atomic commits here — wallet balance + ledger + order

    except OrderServiceError:
        stock.release_stock()
        raise

    except Exception as exc:
        stock.release_stock()
        # Unique-constraint violation on (reseller_id, client_order_id) means
        # a concurrent request committed the same client_order_id first.
        if _pgcompat.is_unique_violation(exc) and validated.client_order_id:
            existing_id = _get_existing_order_id(
                validated.reseller_id, validated.client_order_id
            )
            if existing_id is not None:
                return OrderResult(
                    order_id=existing_id,
                    wallet_transaction_id=_last_purchase_debit_id(validated.reseller_id),
                    wallet_reference=wallet_reference,
                    is_duplicate=True,
                )
        raise OrderServiceError(
            "order_creation_failed",
            "Order could not be created due to a database error.",
            500,
        ) from exc

    return OrderResult(
        order_id=order_id,
        wallet_transaction_id=wallet_tx_id,
        wallet_reference=wallet_reference,
        is_duplicate=False,
        created_at=_created,
    )


__all__ = [
    "ensure_order_metadata_columns",
    "OrderServiceError",
    "OrderResult",
    "create_reseller_order",
]
