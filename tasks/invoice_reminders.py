"""Abandoned-invoice reminder.

Sends a one-time nudge ~1 hour before a pending payment invoice expires so
buyers can finish paying. Each invoice is reminded at most once, tracked by
the deposits.reminded flag, so users are never spammed.
"""

import html
from datetime import timedelta

from telegram import InlineKeyboardMarkup
from utils.button_styles import AutoStyledButton as InlineKeyboardButton

from utils.error_logger import log_error
from utils.premium_emoji import strip_premium
from utils.timezone import now
from database.db import cur, conn
from config import DEPOSIT_EXPIRE_HOURS

# Send the reminder this many hours before the invoice expires.
REMIND_BEFORE_HOURS = 1


async def run_invoice_reminders(context):
    """Find pending invoices close to expiry and send a single reminder each."""
    try:
        remind_after_hours = max(0, float(DEPOSIT_EXPIRE_HOURS) - REMIND_BEFORE_HOURS)
        remind_before = (now() - timedelta(hours=remind_after_hours)).strftime("%Y-%m-%d %H:%M:%S")
        expire_cutoff = (now() - timedelta(hours=float(DEPOSIT_EXPIRE_HOURS))).strftime("%Y-%m-%d %H:%M:%S")
        cur.execute(
            """
            SELECT id, user_id, amount, unique_amount, checkout_product_id, checkout_quantity
            FROM deposits
            WHERE status='pending'
              AND COALESCE(reminded, 0) = 0
              AND created <= ?
              AND created > ?
            ORDER BY id ASC
            """,
            (remind_before, expire_cutoff),
        )
        rows = cur.fetchall()
    except Exception as exc:
        log_error("Invoice reminder query failed", exc)
        return

    for dep_id, uid, amount, unique_amount, checkout_product_id, checkout_quantity in rows:
        # Flag first so a send failure can never cause repeat reminders.
        try:
            cur.execute("UPDATE deposits SET reminded=1 WHERE id=? AND status='pending'", (dep_id,))
            conn.commit()
        except Exception as exc:
            log_error("Invoice reminder flag failed", exc, extra=f"deposit={dep_id}")
            continue

        pay_amount = unique_amount if unique_amount else amount

        if checkout_product_id and int(checkout_quantity or 0) > 0:
            try:
                cur.execute("SELECT name FROM products WHERE id=?", (int(checkout_product_id),))
                prod = cur.fetchone()
            except Exception:
                prod = None
            pname = prod[0] if prod else "your order"
            pretty = html.escape(strip_premium(str(pname)), quote=False)
            body = (
                "\u23f3 <b>Your order is about to expire!</b>\n\n"
                f"\U0001f9fe Invoice <b>#{dep_id}</b> \u2014 <b>{pretty}</b> x{int(checkout_quantity)}\n"
                f"\U0001f4b0 Send exactly <code>{float(pay_amount):.5f} USDT</code>\n\n"
                "This invoice expires in about <b>1 hour</b>. Finish the payment and your "
                "codes are delivered automatically. \U0001f511"
            )
        else:
            body = (
                "\u23f3 <b>Payment reminder</b>\n\n"
                f"\U0001f9fe Invoice <b>#{dep_id}</b> for <b>{float(amount):.2f} USDT</b> is still pending.\n"
                f"\U0001f4b0 Send exactly <code>{float(pay_amount):.5f} USDT</code>\n\n"
                "It expires in about <b>1 hour</b>. Complete it to top up your wallet. \U0001f4bc"
            )

        try:
            await context.bot.send_message(
                chat_id=uid,
                text=body,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("\U0001f504 Deposit History", callback_data="deposit_history")]]
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            log_error("Failed to send invoice reminder", exc, extra=f"user={uid} deposit={dep_id}")
