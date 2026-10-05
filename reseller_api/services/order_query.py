"""Read-only order query service — Phase 7 Prompt 7.

All queries are scoped to the authenticated reseller_id from the API context.
The client NEVER supplies reseller identity; it is always taken from g.api_context.

IDOR prevention
---------------
Both list and detail queries include `AND reseller_id=? AND source='reseller_api'`
in every WHERE clause. A reseller cannot retrieve another reseller's orders even
if they guess or brute-force a valid internal or public order ID.

Filters implemented (index-appropriate only)
--------------------------------------------
Filter          | DB index used
----------------|------------------------------------------
after_id        | PRIMARY KEY (id) — cursor pagination
product_id      | idx_orders_product (product_id)
client_order_id | idx_orders_reseller_idempotency (reseller_id, client_order_id)
date_from/to    | idx_orders_created (created)

Status filtering is NOT implemented: order status is computed by
get_order_status_label() (one DB call per row) with no persisted column
or index. Applying a status filter would require fetching all rows for the
reseller and evaluating every one — unsafe for accounts with many orders.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from database.db import get_cursor, get_order_status_label
from database.reseller_db import ensure_product_public_id, from_cents
from reseller_api.context import ApiRequestContext


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_LIMIT      = 20
MAX_LIMIT           = 100
MAX_CLIENT_OIDLEN   = 128

_PUBLIC_ORDER_ID_RE = re.compile(r'^ORD-(\d{8})$')
_DATE_RE            = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_PUBLIC_PRODUCT_RE  = re.compile(r'^PRD-[A-HJ-NP-Z2-9]{12}$')

# These params must never be accepted from the client.
_FORBIDDEN_PARAMS = frozenset({
    'reseller_id', 'reseller', 'reseller_public_id',
    'user_id', 'source',
})
_ALLOWED_LIST_PARAMS = frozenset({
    'limit', 'after_id', 'product_id', 'client_order_id',
    'date_from', 'date_to',
})


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------

class OrderQueryError(ValueError):
    """Raised when an order query request is invalid or forbidden."""
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(code)
        self.code    = str(code)
        self.message = str(message)
        self.status  = int(status)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_public_order_id(raw: str) -> int:
    """Parse 'ORD-00000012' → 12.  Raises OrderQueryError on bad format."""
    m = _PUBLIC_ORDER_ID_RE.match(str(raw or '').strip())
    if not m:
        raise OrderQueryError(
            'invalid_order_id',
            'Order ID format is invalid. Expected ORD-XXXXXXXX.',
            400,
        )
    return int(m.group(1))


def _resolve_product_internal_id(public_product_id: str) -> Optional[int]:
    """Return internal product id for a valid, active public product ID."""
    cur = get_cursor()
    cur.execute(
        "SELECT id FROM products WHERE public_id=?",
        (public_product_id,),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def _api_status(order_id: int) -> str:
    """Map the master status label to a clean API string."""
    try:
        label = get_order_status_label(int(order_id))
        if 'Replaced' in label:
            return 'replaced'
        if 'Warranty' in label or 'warranty' in label:
            return 'warranty_review'
        return 'completed'
    except Exception:
        return 'completed'


def _format_row(row, include_delivery_status: bool = True) -> Dict[str, Any]:
    """Convert a raw DB row to a clean public-facing order dict.

    Row columns (positional):
        0  id
        1  product_id
        2  product_name
        3  price          (REAL total, legacy)
        4  quantity       (INTEGER, may be NULL for legacy rows)
        5  unit_price_cents (INTEGER, may be NULL for legacy rows)
        6  created
        7  client_order_id
        8  delivery       (encrypted blob, only used for delivery_status)

    Fields intentionally excluded
    -----------------------------
    - delivery content (encrypted credentials / codes)
    - reseller_id, reseller_key_id (other reseller information)
    - user_id (internal admin field)
    - source (internal routing tag)
    - price (REAL legacy column — superseded by unit_price_cents)
    """
    (
        order_id, product_id, product_name, price_real,
        quantity, unit_price_cents,
        created, client_order_id, request_id, delivery,
    ) = row

    pub_order_id    = f"ORD-{int(order_id):08d}"
    pub_product_id  = ensure_product_public_id(int(product_id)) if product_id else None
    qty             = int(quantity) if quantity else 1

    # Price display — prefer stored unit_price_cents; fall back to price REAL.
    if unit_price_cents is not None:
        unit_display  = from_cents(int(unit_price_cents))
        total_display = from_cents(int(unit_price_cents) * qty)
    elif price_real is not None:
        total_cents   = round(float(price_real) * 100)
        unit_cents    = total_cents // max(qty, 1)
        unit_display  = from_cents(unit_cents)
        total_display = from_cents(total_cents)
    else:
        unit_display  = '0.00'
        total_display = '0.00'

    result: Dict[str, Any] = {
        'order_id':   pub_order_id,
        'product_id': pub_product_id,
        'quantity':   qty,
        'unit_price': unit_display,
        'total':      total_display,
        'currency':   'USDT',
        'status':     _api_status(int(order_id)),
        'created_at': created,
        'updated_at': None,        # master schema has no updated column
    }
    if client_order_id:
        result['client_order_id'] = client_order_id
    if include_delivery_status:
        result['delivery_status'] = 'delivered' if delivery else 'pending'
    return result


# ---------------------------------------------------------------------------
# Parameter parsing
# ---------------------------------------------------------------------------

def parse_order_list_params(args) -> Dict[str, Any]:
    """Validate and normalise GET /api/v1/orders query parameters.

    Args:
        args: A dict-like object (Flask request.args or plain dict for tests).

    Returns:
        Validated parameter dict.

    Raises:
        OrderQueryError: On any validation failure.
    """
    # Reject identity-injection attempts regardless of other validation.
    if set(args.keys()) & _FORBIDDEN_PARAMS:
        raise OrderQueryError(
            'invalid_parameter',
            'Reseller identity is determined by the authenticated API key.',
        )
    unknown = set(args.keys()) - _ALLOWED_LIST_PARAMS
    if unknown:
        raise OrderQueryError(
            'invalid_parameter',
            f'Unsupported query parameter(s): {sorted(unknown)}',
        )

    # --- limit ---
    raw_limit = args.get('limit')
    if raw_limit is not None:
        s = str(raw_limit).strip()
        if not s.isdigit() or int(s) < 1:
            raise OrderQueryError('invalid_pagination', 'limit must be a positive integer.')
        limit = int(s)
        if limit > MAX_LIMIT:
            raise OrderQueryError('invalid_pagination', f'limit must not exceed {MAX_LIMIT}.')
    else:
        limit = DEFAULT_LIMIT

    # --- after_id (cursor) ---
    raw_after = args.get('after_id')
    after_id: Optional[int] = None
    if raw_after is not None:
        try:
            after_id = _parse_public_order_id(str(raw_after).strip())
        except OrderQueryError:
            raise OrderQueryError(
                'invalid_pagination',
                'after_id must be a valid order ID (ORD-XXXXXXXX).',
            )

    # --- product_id ---
    raw_pid = args.get('product_id')
    product_id_filter: Optional[str] = None
    if raw_pid is not None:
        pid = str(raw_pid).strip()
        if not _PUBLIC_PRODUCT_RE.match(pid):
            raise OrderQueryError(
                'invalid_filter',
                'product_id must be a valid public product ID (PRD-...).',
            )
        product_id_filter = pid

    # --- client_order_id ---
    raw_coid = args.get('client_order_id')
    coid_filter: Optional[str] = None
    if raw_coid is not None:
        coid_filter = str(raw_coid).strip()
        if len(coid_filter) > MAX_CLIENT_OIDLEN:
            raise OrderQueryError('invalid_filter', 'client_order_id is too long.')
        if not coid_filter:
            raise OrderQueryError('invalid_filter', 'client_order_id must not be empty.')

    # --- date range ---
    raw_df = args.get('date_from')
    date_from: Optional[str] = None
    if raw_df is not None:
        df = str(raw_df).strip()
        if not _DATE_RE.match(df):
            raise OrderQueryError('invalid_filter', 'date_from must be YYYY-MM-DD.')
        date_from = df

    raw_dt = args.get('date_to')
    date_to: Optional[str] = None
    if raw_dt is not None:
        dt = str(raw_dt).strip()
        if not _DATE_RE.match(dt):
            raise OrderQueryError('invalid_filter', 'date_to must be YYYY-MM-DD.')
        date_to = dt

    if date_from and date_to and date_from > date_to:
        raise OrderQueryError('invalid_filter', 'date_from must not be later than date_to.')

    return {
        'limit':          limit,
        'after_id':       after_id,
        'product_id':     product_id_filter,
        'client_order_id': coid_filter,
        'date_from':      date_from,
        'date_to':        date_to,
    }


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def get_order_list(
    context: ApiRequestContext,
    params: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return a paginated list of orders for the authenticated reseller.

    Always applies WHERE reseller_id = <auth_context.reseller_id> so a reseller
    can only see their own orders regardless of any client-supplied parameters.

    Returns:
        (orders, pagination_meta)
    """
    cur         = get_cursor()
    reseller_id = context.reseller_id
    limit       = params['limit']

    # Resolve public product_id to internal id if a filter was requested.
    internal_product_id: Optional[int] = None
    if params.get('product_id'):
        internal_product_id = _resolve_product_internal_id(params['product_id'])
        if internal_product_id is None:
            # Unknown/inaccessible product → empty result (no enumeration oracle).
            return [], {'has_more': False, 'next_after_id': None}

    where_clauses: List[str] = ["reseller_id=?", "source='reseller_api'"]
    bindings: List[Any]      = [reseller_id]

    # Cursor pagination: walk backwards through id (DESC) so after_id is
    # the last id seen by the client.
    if params.get('after_id') is not None:
        where_clauses.append("id < ?")
        bindings.append(params['after_id'])

    if internal_product_id is not None:
        where_clauses.append("product_id=?")
        bindings.append(internal_product_id)

    if params.get('client_order_id') is not None:
        where_clauses.append("client_order_id=?")
        bindings.append(params['client_order_id'])

    if params.get('date_from'):
        where_clauses.append("date(created) >= date(?)")
        bindings.append(params['date_from'])

    if params.get('date_to'):
        where_clauses.append("date(created) <= date(?)")
        bindings.append(params['date_to'])

    sql = """
        SELECT id, product_id, product_name, price,
               quantity, unit_price_cents,
               created, client_order_id, request_id, delivery
        FROM   orders
        WHERE  {where}
        ORDER  BY id DESC
        LIMIT  ?
    """.format(where=' AND '.join(where_clauses))
    bindings.append(limit + 1)   # fetch one extra to detect has_more

    cur.execute(sql, bindings)
    rows = cur.fetchall()

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    orders = [_format_row(r) for r in rows]

    next_after_id: Optional[str] = None
    if has_more and rows:
        next_after_id = f"ORD-{int(rows[-1][0]):08d}"

    return orders, {'has_more': has_more, 'next_after_id': next_after_id}


def get_order_detail(
    context: ApiRequestContext,
    public_order_id: str,
) -> Dict[str, Any]:
    """Return a single order, ONLY if it belongs to the authenticated reseller.

    IDOR prevention: The WHERE clause always includes reseller_id=? scoped
    to the auth context. A valid order ID for another reseller returns 404
    — indistinguishable from an invalid ID to prevent oracle attacks.
    """
    internal_id = _parse_public_order_id(public_order_id)   # may raise

    cur = get_cursor()
    cur.execute(
        """
        SELECT id, product_id, product_name, price,
               quantity, unit_price_cents,
               created, client_order_id, request_id, delivery
        FROM   orders
        WHERE  id=?
          AND  reseller_id=?
          AND  source='reseller_api'
        """,
        (internal_id, context.reseller_id),
    )
    row = cur.fetchone()
    if row is None:
        raise OrderQueryError('order_not_found', 'Order not found.', 404)

    return _format_row(row, include_delivery_status=True)


__all__ = [
    'OrderQueryError',
    'parse_order_list_params',
    'get_order_list',
    'get_order_detail',
]
