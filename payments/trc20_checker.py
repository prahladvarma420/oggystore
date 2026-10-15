"""USDT TRC20 (Tron) auto-payment checker.

Works exactly like the BEP20 checker: customers send an exact unique amount
to the shop's TRC20 address (Trust Wallet etc.) and this job matches incoming
transfers against pending invoices using the free TronGrid public API.
No API key is required; set TRONGRID_API_KEY for higher rate limits.
"""

from payments.deposit_settlement import settle_customer_safely as settle_customer
from payments import reseller_deposits as reseller_deps

from datetime import timedelta
from decimal import Decimal

import html

import httpx

from utils.crypto import encrypt_text
from utils.premium_emoji import strip_premium
from utils.delivery_file import is_bulk_delivery, send_delivery_txt, bot_document_sender, checkout_codes_section
from utils.error_logger import log_error
from utils.timezone import now, now_text
from database.db import cur, conn, get_balance, render_message_template, reserve_stock_items, get_available_stock_count
from config import (
    ADMIN_ID,
    USDT_TRC20_ADDRESS,
    USDT_TRC20_CONTRACT,
    USDT_TRC20_DECIMALS,
    TRONGRID_API_KEY,
    DEPOSIT_EXPIRE_HOURS,
    PAYMENT_AMOUNT_TOLERANCE,
)

DEPOSIT_SUCCESS_DEFAULT = (
    "\u2705 <b>DEPOSIT CONFIRMED</b>\n\n\U0001f9fe <b>Invoice:</b> <code>#{invoice_id}</code>\n\U0001f4b5 <b>Paid:</b> <code>{paid} USDT</code>\n\U0001f4b0 <b>Balance Added:</b> <code>{amount} USDT</code>\n\U0001f4bc <b>New Balance:</b> <code>{balance} USDT</code>\n\U0001f517 <b>TX:</b> {tx}\n\n\U0001f6cd <i>You can now buy products from Shop.</i>"
)

TRONGRID_BASE = "https://api.trongrid.io"
TRONSCAN_TX_BASE = "https://tronscan.org/#/transaction/"


def _config_ready() -> bool:
    return (
        isinstance(USDT_TRC20_ADDRESS, str)
        and USDT_TRC20_ADDRESS.startswith("T")
        and len(USDT_TRC20_ADDRESS) >= 30
    )


def _pending_trc20_deposits():
    cutoff = (now() - timedelta(hours=DEPOSIT_EXPIRE_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """
        SELECT id, user_id, amount, unique_amount, checkout_product_id, checkout_quantity, checkout_done
        FROM deposits
        WHERE status='pending' AND method='trc20' AND created >= ?
        ORDER BY id ASC
        """,
        (cutoff,),
    )
    return cur.fetchall()


def _not_processed(tx_hash: str) -> bool:
    cur.execute("SELECT 1 FROM processed_txs WHERE tx_hash=?", (tx_hash,))
    return cur.fetchone() is None


def _matches_amount(paid: Decimal, expected: float) -> bool:
    return abs(paid - Decimal(str(expected))) <= Decimal(str(PAYMENT_AMOUNT_TOLERANCE))


async def _recent_incoming_transfers():
    """Latest confirmed incoming USDT TRC20 transfers to the shop wallet."""
    url = TRONGRID_BASE + "/v1/accounts/" + USDT_TRC20_ADDRESS + "/transactions/trc20"
    params = {
        "only_confirmed": "true",
        "only_to": "true",
        "limit": "50",
        "contract_address": USDT_TRC20_CONTRACT,
    }
    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    transfers = []
    for tx in data.get("data", []) or []:
        try:
            if str(tx.get("to", "")) != str(USDT_TRC20_ADDRESS):
                continue
            token = tx.get("token_info") or {}
            if str(token.get("address", "")) != str(USDT_TRC20_CONTRACT):
                continue
            decimals = int(token.get("decimals", -1))
            if decimals != USDT_TRC20_DECIMALS:
                continue
            paid = Decimal(str(tx.get("value", "0"))) / (Decimal(10) ** Decimal(decimals))
            tx_id = str(tx.get("transaction_id", "") or "")
            if tx_id and paid > 0:
                transfers.append((tx_id, paid))
        except Exception:
            continue
    return transfers


async def _deliver_trc20_checkout(context, deposit_id, user_id, base_amount, paid_amount, product_id, quantity, tx_id, tx_link, short_tx):
    """Deliver a product bought directly via TRC20 checkout (mirrors BEP20)."""
    settled = settle_customer(deposit_id, 'trc20', tx_id, paid_amount)
    if not settled["posted"]: return
    pass  # product snapshot from settlement
    product = (settled['product_name'],) if settled['product_exists'] else None

    if not product:
        # Product no longer available -> credit the wallet instead.
        pass  # settled atomically above
        pass  # financial mutation in settlement
        pass  # financial mutation in settlement
        pass  # no independent payment commit
        return

    product_name = product[0]
    deliveries = settled['deliveries']

    if deliveries:
        delivery_text = "\n\n".join([str(x) for x in deliveries])
        remaining_stock = get_available_stock_count(product_id)
        pass  # financial mutation in settlement
        try:
            from payments.bep20_checker import _maybe_low_stock_alert
            await _maybe_low_stock_alert(context, product_id)
        except Exception:
            pass
        pass  # financial mutation in settlement
        order_id = settled['order_id']
        pass  # financial mutation in settlement
        pass  # financial mutation in settlement
        pass  # no independent payment commit

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "\u2705 <b>Payment Successful!</b>\n"
                    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
                    f"\U0001f9fe <b>Order ID:</b> <code>#{order_id}</code>\n\n"
                    f"\U0001f4e6 <b>{html.escape(strip_premium(product_name), quote=False)}</b>\n"
                    f"\U0001f522 <b>Quantity:</b> <code>{quantity}</code>\n"
                    f"\U0001f4b0 <b>Paid:</b> <code>${float(base_amount):.2f}</code>\n"
                    f"\U0001f517 <b>TX:</b> <a href='{tx_link}'>{short_tx}</a>\n\n"
                    + checkout_codes_section(quantity, delivery_text) +
                    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                    "Thank you for your purchase! \U0001f389"
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            log_error("Failed to send TRC20 checkout delivery", exc, extra=f"user={user_id} deposit={deposit_id}")

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
                log_error("Failed to send TRC20 checkout delivery file", file_exc, extra=f"user={user_id} deposit={deposit_id}")

        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "\U0001f6d2 <b>DIRECT CHECKOUT CONFIRMED (TRC20)</b>\n\n"
                    f"\U0001f464 User: <code>{user_id}</code>\n"
                    f"\U0001f9fe Invoice: <code>#{deposit_id}</code>\n"
                    f"\U0001f4e6 Product: <b>{html.escape(strip_premium(product_name), quote=False)}</b>\n"
                    f"\U0001f522 Quantity: <code>{quantity}</code>\n"
                    f"\U0001f4b0 Paid: <code>{float(base_amount):.2f} USDT</code>\n"
                    f"\U0001f517 TX: <a href='{tx_link}'>{short_tx}</a>"
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            pass
    else:
        # Out of stock at delivery time -> credit the wallet instead.
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


async def check_trc20_payments(context):
    await reseller_deps.post_credit_actions(context)
    if not _config_ready():
        return

    pending = _pending_trc20_deposits()
    if not pending and not reseller_deps.has_receiving_records("trc20"):
        await reseller_deps.post_credit_actions(context)
        return

    try:
        transfers = await _recent_incoming_transfers()
    except Exception as exc:
        print("[trc20scan] transfer fetch failed", flush=True)
        log_error("TRC20 payment checker fetch error", extra="category=provider_unavailable")
        return

    await reseller_deps.process_observations(context,"trc20",[(tx,paid,"") for tx,paid in transfers])

    for deposit_id, user_id, base_amount, unique_amount, checkout_product_id, checkout_quantity, checkout_done in pending:
        for tx_id, paid_amount in transfers:
            if not tx_id or not _not_processed(tx_id):
                continue
            if not _matches_amount(paid_amount, unique_amount):
                continue

            tx_link = TRONSCAN_TX_BASE + tx_id
            short_tx = tx_id[:10] + "..." + tx_id[-8:]

            # Direct product checkout paid with TRC20 -> deliver the codes.
            if checkout_product_id and int(checkout_quantity or 0) > 0 and not checkout_done:
                await _deliver_trc20_checkout(
                    context, deposit_id, user_id, base_amount, paid_amount,
                    int(checkout_product_id), int(checkout_quantity),
                    tx_id, tx_link, short_tx,
                )
                break

            # Plain wallet top-up.
            settled = settle_customer(deposit_id, 'trc20', tx_id, paid_amount)
            if not settled["posted"]: continue

            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=render_message_template(
                        "deposit_success",
                        DEPOSIT_SUCCESS_DEFAULT,
                        invoice_id=deposit_id,
                        paid=f"{paid_amount:.5f}",
                        amount=f"{float(base_amount):.2f}",
                        balance=f"{get_balance(user_id):.2f}",
                        tx=f"<a href='{tx_link}'>{short_tx}</a>",
                    ),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                log_error("Failed to notify user about TRC20 deposit", exc, extra=f"user={user_id} deposit={deposit_id}")

            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "\U0001f4b0 <b>AUTO PAYMENT CONFIRMED (TRC20)</b>\n\n"
                        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
                        f"\U0001f464 <b>User:</b> <code>{user_id}</code>\n"
                        f"\U0001f9fe <b>Invoice:</b> <code>#{deposit_id}</code>\n"
                        f"\U0001f4b5 <b>Paid:</b> <code>{paid_amount:.5f} USDT</code>\n"
                        f"\U0001f4b0 <b>Credit:</b> <code>{float(base_amount):.2f} USDT</code>\n"
                        f"\U0001f517 <b>TX:</b> <a href='{tx_link}'>{short_tx}</a>"
                    ),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

            break
