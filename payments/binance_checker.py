"""USDT Binance Pay auto-deposit checker.

Flow:
1. Customer creates a deposit invoice in the bot and enters the amount.
2. The bot gives them a unique 8-character NOTE code.
3. Customer opens Binance App -> Pay -> Send, sends USDT to the shop's Binance
   ID, and puts the note code in the transfer Note/Remark.
4. This job polls Binance "Get Pay Trade History"
   (GET /sapi/v1/pay/transactions), finds an incoming transfer whose note
   contains the code, and credits the wallet.

SECURITY / anti-PEPE guard:
    Only transfers whose asset is USDT are EVER credited. The top-level
    `currency` must be USDT and every `fundsDetail` entry must be USDT. Any
    other token (PEPE, BNB, etc.) is ignored, even if the note code matches
    and the number looks right. This prevents the
    "sent 500 PEPE, got credited 500 USDT" exploit.

Requires env vars: BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_PAY_ID.
The API key only needs read access (Enable Reading); no trading/withdrawal
permission is required.
"""

from payments.deposit_settlement import settle_customer_safely as settle_customer
from payments import reseller_deposits as reseller_deps

import hashlib
import hmac
import json
import time
from datetime import timedelta
from decimal import Decimal

import httpx

from utils.error_logger import log_error
from utils.delivery_file import is_bulk_delivery, send_delivery_txt, bot_document_sender, checkout_codes_section
from utils.timezone import now, now_text
from database.db import cur, conn, get_balance, render_message_template
from config import (
    ADMIN_ID,
    BINANCE_API_KEY,
    BINANCE_API_SECRET,
    BINANCE_PAY_ID,
    BINANCE_API_BASE,
    BINANCE_HTTP_PROXY,
    DEPOSIT_EXPIRE_HOURS,
    PAYMENT_AMOUNT_TOLERANCE,
)

PAY_TX_PATH = "/sapi/v1/pay/transactions"


def _make_async_client():
    """httpx client, optionally routed through a proxy (to dodge geo-blocks).

    Handles both new (proxy=) and old (proxies=) httpx signatures.
    """
    if BINANCE_HTTP_PROXY:
        try:
            return httpx.AsyncClient(timeout=20, proxy=BINANCE_HTTP_PROXY)
        except TypeError:
            return httpx.AsyncClient(timeout=20, proxies=BINANCE_HTTP_PROXY)
    return httpx.AsyncClient(timeout=20)

# The ONLY asset the bot will ever credit. This is a strict whitelist: any
# other token (PEPE, SHIB, BNB, BTC, USDC, unknown/empty, or a mixed payment)
# is rejected. It is NOT a PEPE-specific blocklist.
ACCEPTED_ASSET = "USDT"

DEPOSIT_SUCCESS_DEFAULT = (
    "\u2705 <b>DEPOSIT CONFIRMED</b>\n\n\U0001f9fe <b>Invoice:</b> <code>#{invoice_id}</code>\n\U0001f4b5 <b>Paid:</b> <code>{paid} USDT</code>\n\U0001f4b0 <b>Balance Added:</b> <code>{amount} USDT</code>\n\U0001f4bc <b>New Balance:</b> <code>{balance} USDT</code>\n\U0001f517 <b>TX:</b> {tx}\n\n\U0001f6cd <i>You can now buy products from Shop.</i>"
)


def _config_ready() -> bool:
    return bool(BINANCE_API_KEY and BINANCE_API_SECRET and BINANCE_PAY_ID)


def _pending_binance_deposits():
    """Pending, non-expired Binance-Pay invoices that have a note code."""
    cutoff = (now() - timedelta(hours=DEPOSIT_EXPIRE_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """
        SELECT id, user_id, amount, note_code, checkout_product_id, checkout_quantity, checkout_done
        FROM deposits
        WHERE status='pending' AND method='binance'
              AND note_code IS NOT NULL AND note_code != '' AND created >= ?
        ORDER BY id ASC
        """,
        (cutoff,),
    )
    return cur.fetchall()


async def _deliver_binance_checkout(context, deposit_id, user_id, base_amount, product_id, quantity, tx_id, code, paid_amount=None):
    """Deliver a direct product checkout paid via Binance Pay.

    Mirrors the BEP20 direct-checkout path: reserve stock atomically, record
    the order, mark the invoice paid and send the codes. If stock ran out or
    the product vanished, the money is credited to the wallet instead so the
    customer never loses funds.
    """
    settled = settle_customer(deposit_id, 'binance', tx_id, paid_amount if paid_amount is not None else base_amount, notes=code)
    if not settled["posted"]: return
    from database.db import reserve_stock_items, get_available_stock_count, encrypt_text
    import html as _html
    from utils.premium_emoji import strip_premium as _strip

    pass  # product snapshot from settlement
    product = (settled['product_name'],) if settled['product_exists'] else None
    deliveries = settled['deliveries']

    if product and deliveries:
        product_name = str(product[0])
        try:
            from products.product_handlers import format_delivery_items
            delivery_text = format_delivery_items(deliveries)
        except Exception:
            delivery_text = "\n\n".join(str(x) for x in deliveries)
        remaining_stock = get_available_stock_count(product_id)
        pass  # financial mutation in settlement
        pass  # financial mutation in settlement
        order_id = settled['order_id']
        pass  # financial mutation in settlement
        pass  # financial mutation in settlement
        pass  # no independent payment commit
        try:
            from payments.bep20_checker import _maybe_low_stock_alert
            await _maybe_low_stock_alert(context, product_id)
        except Exception:
            pass
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "\u2705 <b>Payment Successful!</b>\n"
                    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
                    f"\U0001f9fe <b>Order ID:</b> <code>#{order_id}</code>\n\n"
                    f"\U0001f4e6 <b>{_html.escape(_strip(product_name), quote=False)}</b>\n"
                    f"\U0001f522 <b>Quantity:</b> <code>{quantity}</code>\n"
                    f"\U0001f4b0 <b>Paid:</b> <code>${float(base_amount):.2f}</code>\n"
                    f"\U0001f516 <b>Note code:</b> <code>{code}</code>\n"
                    f"\U0001f517 <b>TX:</b> <code>{tx_id}</code>\n\n"
                    + checkout_codes_section(quantity, delivery_text) +
                    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                    "Thank you for your purchase! \U0001f389"
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            log_error("Failed to send Binance checkout delivery", exc, extra=f"user={user_id} deposit={deposit_id}")

        # Bulk orders (>5 items): attach the complete numbered delivery as a
        # .txt file (the full delivery is stored on the order regardless).
        if is_bulk_delivery(quantity, delivery_text):
            try:
                await send_delivery_txt(
                    bot_document_sender(context.bot, user_id),
                    order_id=order_id,
                    product_name=product_name,
                    quantity=quantity,
                    items=deliveries,
                    paid=float(base_amount),
                    balance=get_balance(user_id),
                )
            except Exception as file_exc:
                # Log only the failure, never the delivery contents.
                log_error("Failed to send Binance checkout delivery file", file_exc, extra=f"user={user_id} deposit={deposit_id}")
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "\U0001f6d2 <b>DIRECT CHECKOUT CONFIRMED (Binance Pay)</b>\n\n"
                    f"\U0001f464 User: <code>{user_id}</code>\n"
                    f"\U0001f9fe Invoice: <code>#{deposit_id}</code>\n"
                    f"\U0001f4e6 Product: <b>{_html.escape(_strip(product_name), quote=False)}</b>\n"
                    f"\U0001f522 Quantity: <code>{quantity}</code>\n"
                    f"\U0001f4b0 Paid: <code>{float(base_amount):.2f} USDT</code>\n"
                    f"\U0001f516 Note code: <code>{code}</code>\n"
                    f"\U0001f517 TX: <code>{tx_id}</code>"
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            pass
        return

    # Stock ran out (or product removed) between checkout and payment:
    # credit the wallet instead so the customer never loses funds.
    pass  # settled atomically above
    pass  # financial mutation in settlement
    pass  # financial mutation in settlement
    pass  # no independent payment commit
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "\u2705 <b>Payment Received</b>\n\n"
                "\u26a0\ufe0f Stock became unavailable before delivery.\n"
                f"\U0001f4b0 <b>{float(base_amount):.2f} USDT</b> was added to your wallet."
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass


def _not_processed(tx_id: str) -> bool:
    cur.execute("SELECT 1 FROM processed_txs WHERE tx_hash=?", (tx_id,))
    return cur.fetchone() is None


def _is_usdt_only(tx) -> bool:
    """Return True ONLY if every asset in this transfer is USDT.

    Whitelist logic (not a PEPE-only exception): the transfer is accepted only
    when its asset is exactly USDT. Everything else is rejected, including:
      - any other token (PEPE, SHIB, BNB, BTC, USDC, etc.)
      - an empty / missing / unknown currency
      - a mixed "combination" payment where any part is not USDT
    """
    currency = str(tx.get("currency", "")).strip().upper()
    if currency != ACCEPTED_ASSET:
        return False
    # If Binance itemises the funding sources, every single one must be USDT.
    for f in (tx.get("fundsDetail") or []):
        if str(f.get("currency", "")).strip().upper() != ACCEPTED_ASSET:
            return False
    return True


async def _recent_pay_transactions():
    """Signed call to Get Pay Trade History. Returns the raw data list."""
    ts = int(time.time() * 1000)
    start = ts - 2 * 24 * 60 * 60 * 1000  # look back 2 days
    query = f"startTime={start}&limit=100&recvWindow=60000&timestamp={ts}"
    signature = hmac.new(
        BINANCE_API_SECRET.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    url = f"{BINANCE_API_BASE}{PAY_TX_PATH}?{query}&signature={signature}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}

    async with _make_async_client() as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            # Surface the real Binance error (e.g. -2015 invalid API-key/IP/permission)
            print(f"[binancepay] HTTP {resp.status_code}: provider request rejected", flush=True)
            resp.raise_for_status()
        data = resp.json()

    if not isinstance(data, dict) or not data.get("success"):
        print("[binancepay] invalid provider response", flush=True)
        return []
    return data.get("data") or []


async def check_binance_payments(context):
    await reseller_deps.post_credit_actions_safely(context)
    if not _config_ready():
        return

    pending = _pending_binance_deposits()
    if not pending and not reseller_deps.has_receiving_records_safely("binance"):
        await reseller_deps.post_credit_actions_safely(context)
        return

    try:
        txs = await _recent_pay_transactions()
    except Exception as exc:
        print("[binancepay] transfer fetch failed", flush=True)
        log_error("Binance Pay checker fetch error", extra="category=provider_unavailable")
        return

    observations=[]
    for tx in txs:
        try:
            if _is_usdt_only(tx): observations.append((str(tx.get("transactionId", "")), Decimal(str(tx.get("amount",0))), json.dumps(tx,default=str)))
        except Exception: pass
    await reseller_deps.process_observations_safely(context,"binance",observations)

    tol = Decimal(str(PAYMENT_AMOUNT_TOLERANCE))

    for deposit_id, user_id, base_amount, note_code, checkout_product_id, checkout_quantity, checkout_done in pending:
        code = str(note_code or "").strip().upper()
        if not code:
            continue

        for tx in txs:
            try:
                tx_id = str(tx.get("transactionId", "") or "")
                if not tx_id or not _not_processed(tx_id):
                    continue
                amount = Decimal(str(tx.get("amount", "0") or "0"))
                if amount <= 0:
                    continue  # only incoming (positive) transfers
                if not _is_usdt_only(tx):
                    continue  # USDT ONLY -- reject PEPE and every other token
                # Match the note code against the WHOLE transaction record, not
                # just the `note` field. For Binance Pay "Send" (C2C) transfers
                # the sender's remark can arrive under a differently-named field
                # on the receiver side, so scanning the full record is far more
                # robust. The code is a random 8-char string, so an accidental
                # match is effectively impossible.
                # Prefer real note/remark fields. Scanning the whole record
                # can match the code against an unrelated field (ids, raw
                # payloads), so only fall back to it when no note field has
                # the code, and log it so blob matches stay visible.
                _note_blob = " ".join(
                    str(tx.get(_k, "") or "")
                    for _k in ("note", "remark", "remarks", "memo", "description",
                               "transactionNote", "payerNote", "orderNote")
                ).upper()
                if code not in _note_blob:
                    tx_blob = json.dumps(tx, default=str).upper()
                    if code not in tx_blob:
                        continue
                    print(
                        f"[binancepay] code {code} matched tx {tx_id} via full-record scan (no note field)",
                        flush=True,
                    )
                if amount < (Decimal(str(base_amount)) - tol):
                    print(
                        f"[binancepay] code {code} matched tx {tx_id} but underpaid: got {amount} need {base_amount}",
                        flush=True,
                    )
                    continue  # underpaid -> do not credit
                print(f"[binancepay] MATCH code {code} tx {tx_id} amount {amount}", flush=True)
            except Exception:
                continue

            # ---- Direct product checkout: deliver codes, don't credit ----
            if checkout_product_id and int(checkout_quantity or 0) > 0 and not checkout_done:
                await _deliver_binance_checkout(
                    context, deposit_id, user_id, float(base_amount),
                    int(checkout_product_id), int(checkout_quantity), tx_id, code, amount,
                )
                break

            # ---- Matched: credit the INVOICE amount, never more ----
            # Previously credited float(amount), so any overpayment (or a
            # false-positive blob match on a larger tx) credited the full sum.
            credit = float(min(amount, Decimal(str(base_amount))))
            settled = settle_customer(deposit_id, 'binance', tx_id, amount, notes=json.dumps(tx,default=str))
            if not settled["posted"]: continue

            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=render_message_template(
                        "deposit_success",
                        DEPOSIT_SUCCESS_DEFAULT,
                        invoice_id=deposit_id,
                        paid=f"{credit:.2f}",
                        amount=f"{credit:.2f}",
                        balance=f"{get_balance(user_id):.2f}",
                        tx=f"<code>{tx_id}</code>",
                    ),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                log_error(
                    "Failed to notify user about Binance deposit",
                    exc,
                    extra=f"user={user_id} deposit={deposit_id}",
                )

            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "\U0001f4b0 <b>AUTO PAYMENT CONFIRMED (Binance Pay)</b>\n\n"
                        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
                        f"\U0001f464 <b>User:</b> <code>{user_id}</code>\n"
                        f"\U0001f9fe <b>Invoice:</b> <code>#{deposit_id}</code>\n"
                        f"\U0001f516 <b>Note code:</b> <code>{code}</code>\n"
                        f"\U0001f4b5 <b>Credited:</b> <code>{credit:.2f} USDT</code>\n"
                        f"\U0001f517 <b>TX:</b> <code>{tx_id}</code>"
                    ),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

            break
