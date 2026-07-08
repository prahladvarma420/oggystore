import random
from database.db import cur, conn, get_balance
from keyboards.menus import deposit_menu, deposit_invoice_menu
from utils.texts import deposit_text
from utils.ui import edit_or_send
from utils.timezone import now_text
from utils.states import user_state
from config import USDT_BEP20_ADDRESS, DEPOSIT_EXPIRE_HOURS, STORE_NAME


async def start_deposit(q, uid):
    user_state[uid] = "deposit_amount"
    await edit_or_send(q, deposit_text(get_balance(uid)), deposit_menu())


async def show_deposit_history(q, uid):
    cur.execute(
        """
        SELECT id, amount, unique_amount, status, created, tx_hash, paid_at
        FROM deposits
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 10
        """,
        (uid,)
    )
    rows = cur.fetchall()

    text = "🧾 <b>Deposit History</b>\n\n"

    if not rows:
        text += "No deposits found yet.\n\nCreate a deposit invoice first."
    else:
        for dep_id, amount, unique_amount, status, created, tx_hash, paid_at in rows:
            if status == "paid":
                status_text = "✅ Paid"
            elif status == "pending":
                status_text = "🟠 Waiting For Payment"
            elif status == "cancelled":
                status_text = "🚫 Cancelled"
            else:
                status_text = f"⚠️ {status.title()}"

            text += (
                f"🧾 <b>Invoice #{dep_id}</b>\n"
                f"💵 Amount: <code>{amount:.2f} USDT</code>\n"
                f"🎯 Exact Pay: <code>{unique_amount:.5f} USDT</code>\n"
                f"📌 Status: <b>{status_text}</b>\n"
            )

            if tx_hash:
                short_tx = tx_hash[:10] + "..." + tx_hash[-8:]
                text += f"🔗 TX: <a href='https://bscscan.com/tx/{tx_hash}'>{short_tx}</a>\n"

            text += "\n"

    if len(text) > 3900:
        text = text[:3900] + "\n..."

    await edit_or_send(q, text, deposit_menu())


async def handle_deposit_amount(update, uid, text):
    try:
        amount = float(text)

        if amount < 0.1 or amount > 5000:
            await update.message.reply_text(
                "❌ <b>Invalid Amount</b>\n\nEnter amount between <code>0.1</code> and <code>5000</code> USDT.",
                parse_mode="HTML"
            )
            return True

        # Cancel older pending invoices for the same user to avoid confusion.
        cur.execute(
            "UPDATE deposits SET status='cancelled' WHERE user_id=? AND status='pending'",
            (uid,)
        )

        unique_amount = round(amount + random.randint(100, 999) / 100000, 5)

        # Set created explicitly in bot time (IST). The DB default is UTC,
        # which made invoices look ~5.5h old and expire after ~30 minutes.
        cur.execute(
            "INSERT INTO deposits(user_id, amount, unique_amount, status, created) VALUES(?,?,?,?,?)",
            (uid, amount, unique_amount, "pending", now_text()),
        )
        conn.commit()

        cur.execute("SELECT last_insert_rowid()")
        invoice_id = cur.fetchone()[0]

        await update.message.reply_text(
            f"""⚡ <b>USDT BEP20 Auto Payment</b>

━━━━━━━━━━━━━━━━━━

💵 <b>Amount:</b> <code>${amount:.2f}</code>
💎 <b>Send exactly:</b> <code>{unique_amount:.5f} USDT</code>
🌐 <b>Network:</b> <b>BNB Smart Chain (BEP20)</b>

━━━━━━━━━━━━━━━━━━

🏧 <b>Wallet address:</b>
<code>{USDT_BEP20_ADDRESS}</code>

━━━━━━━━━━━━━━━━━━

🧾 <b>Invoice ID:</b> <code>#{invoice_id}</code>
🟠 <b>Status:</b> <b>Waiting For Payment</b>
⏳ <b>Expires in:</b> <code>{DEPOSIT_EXPIRE_HOURS} hours</code>

━━━━━━━━━━━━━━━━━━

⚠️ <b>Important</b>
• Send only <b>USDT BEP20</b>.
• Send the <b>exact amount</b> shown above.
• Do not send <code>{amount:.2f} USDT</code>; send <code>{unique_amount:.5f} USDT</code>.
• Your wallet will be credited automatically after confirmation.

✅ <i>No screenshot or TX hash is needed.</i>
🤖 <i>The bot checks blockchain payments automatically.</i>

<b>{STORE_NAME}</b>""",
            reply_markup=deposit_invoice_menu(),
            parse_mode="HTML",
            disable_web_page_preview=True
        )

    except ValueError:
        await update.message.reply_text(
            "❌ <b>Invalid Input</b>\n\nSend only a number. Example: <code>10</code>",
            parse_mode="HTML"
        )
        return True

    finally:
        user_state.pop(uid, None)

    return True
