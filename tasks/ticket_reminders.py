"""24-hour unresolved support-ticket reminders for the admin.

Runs on the existing python-telegram-bot JobQueue (registered in bot.py),
following the same conventions as tasks/invoice_reminders.py.

A ticket is "unresolved" when an admin (or the system) has replied at least
once and the ticket is not closed. A reminder is sent when BOTH the last
activity and the last reminder are at least REMINDER_HOURS old. The reminder
time is persisted in tickets.last_reminder_at, so application restarts and
hourly job ticks never produce duplicates. Closed tickets are never reminded.
"""

import html

from telegram import InlineKeyboardMarkup

from config import ADMIN_ID
from database.db import get_tickets_due_for_reminder, mark_ticket_reminded
from utils.button_styles import AutoStyledButton as InlineKeyboardButton
from utils.timezone import display_time

REMINDER_HOURS = 24


async def run_ticket_reminders(context):
    if not ADMIN_ID:
        return
    try:
        due = get_tickets_due_for_reminder(REMINDER_HOURS)
    except Exception:
        return
    for tid, user_id, first_name, username, subject, status, updated in due:
        customer = first_name or (
            "@" + str(username) if username and username != "No username" else "ID " + str(user_id)
        )
        text = (
            "\U0001f514 <b>Unresolved Ticket Reminder</b>\n\n"
            f"\U0001f3ab <b>Ticket:</b> <code>#{tid}</code>\n"
            f"\U0001f464 <b>Customer:</b> {html.escape(str(customer), quote=False)} (<code>{user_id}</code>)\n"
            f"\U0001f4cc <b>Issue:</b> {html.escape(str(subject or 'Support Ticket'), quote=False)}\n"
            f"\U0001f4ca <b>Status:</b> Unresolved\n"
            f"\U0001f552 <b>Last activity:</b> {display_time(updated)}\n\n"
            "\u23f3 This ticket is still unresolved."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f4ac Reply Ticket", callback_data=f"admin_ticket_reply_{tid}")],
            [InlineKeyboardButton("✅ Close Ticket", callback_data=f"admin_ticket_close_{tid}")],
        ])
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID, text=text, reply_markup=kb, parse_mode="HTML"
            )
            # Only mark AFTER a successful send so a Telegram failure retries
            # on the next hourly tick instead of being lost.
            mark_ticket_reminded(tid)
        except Exception:
            pass
