"""USDT TRC20 (Tron) auto-payment checker.

Works exactly like the BEP20 checker: customers send an exact unique amount
to the shop's TRC20 address (Trust Wallet etc.) and this job matches incoming
transfers against pending invoices using the free TronGrid public API.
No API key is required; set TRONGRID_API_KEY for higher rate limits.
"""

from datetime import timedelta
from decimal import Decimal

import httpx

from utils.error_logger import log_error
from utils.timezone import now, now_text
from database.db import cur, conn, add_balance, get_balance, render_message_template
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
        SELECT id, user_id, amount, unique_amount
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
            decimals = int((tx.get("token_info") or {}).get("decimals", USDT_TRC20_DECIMALS) or USDT_TRC20_DECIMALS)
            paid = Decimal(str(tx.get("value", "0"))) / (Decimal(10) ** Decimal(decimals))
            tx_id = str(tx.get("transaction_id", "") or "")
            if tx_id and paid > 0:
                transfers.append((tx_id, paid))
        except Exception:
            continue
    return transfers


async def check_trc20_payments(context):
    if not _config_ready():
        return

    pending = _pending_trc20_deposits()
    if not pending:
        return

    try:
        transfers = await _recent_incoming_transfers()
    except Exception as exc:
        print(f"[trc20scan] transfer fetch failed: {exc}", flush=True)
        log_error("TRC20 payment checker fetch error", exc)
        return

    print(f"[trc20scan] pending invoices: {[(d[0], d[3]) for d in pending]}", flush=True)

    for deposit_id, user_id, base_amount, unique_amount in pending:
        for tx_id, paid_amount in transfers:
            if not tx_id or not _not_processed(tx_id):
                continue
            if not _matches_amount(paid_amount, unique_amount):
                continue

            add_balance(user_id, float(base_amount))
            cur.execute(
                "UPDATE deposits SET status='paid', tx_hash=?, paid_at=? WHERE id=? AND status='pending'",
                (tx_id, now_text(), deposit_id),
            )
            cur.execute(
                "INSERT OR IGNORE INTO processed_txs(tx_hash, deposit_id, user_id, amount) VALUES(?,?,?,?)",
                (tx_id, deposit_id, user_id, float(paid_amount)),
            )
            conn.commit()

            tx_link = TRONSCAN_TX_BASE + tx_id
            short_tx = tx_id[:10] + "..." + tx_id[-8:]

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
