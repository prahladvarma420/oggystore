import random
from telegram import InlineKeyboardMarkup
from utils.button_styles import AutoStyledButton as InlineKeyboardButton
from database.db import cur, conn, get_balance
from keyboards.menus import deposit_menu, deposit_invoice_menu
from utils.texts import deposit_text
from utils.ui import edit_or_send
from utils.timezone import now_text
from utils.states import user_state
from config import (
    USDT_BEP20_ADDRESS,
    USDT_TRC20_ADDRESS,
    DEPOSIT_EXPIRE_HOURS,
    STORE_NAME,
)


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
                f"💵 Amount: <b>{amount:.2f} USDT</b>\n"
                f"🎯 Exact Pay: <code>{unique_amount:.5f}</code> USDT\n"
                f"📌 Status: <b>{status_text}</b>\n"
            )

            if tx_hash:
                short_tx = tx_hash[:10] + "..." + tx_hash[-8:]
                text += f"🔗 TX: <a href='https://bscscan.com/tx/{tx_hash}'>{short_tx}</a>\n"

            text += "\n"

    if len(text) > 3900:
        text = text[:3900] + "\n..."

    await edit_or_send(q, text, deposit_menu())


def _available_methods():
    """Payment methods that are configured via env vars. BEP20 is the default."""
    methods = []
    if isinstance(USDT_BEP20_ADDRESS, str) and USDT_BEP20_ADDRESS.startswith("0x"):
        methods.append(("bep20", "\U0001f7e1 USDT \u2014 BEP20 (BNB Chain)"))
    if isinstance(USDT_TRC20_ADDRESS, str) and USDT_TRC20_ADDRESS.startswith("T"):
        methods.append(("trc20", "\U0001f534 USDT \u2014 TRC20 (Tron)"))
    return methods


def _method_chooser_text(amount):
    return (
        "\U0001f4b3 <b>Choose Payment Method</b>\n\n"
        f"\U0001f4b5 Amount: <b>{amount:.2f} USDT</b>\n\n"
        "Select how you want to pay \U0001f447"
    )


def _method_menu(amount):
    rows = [
        [InlineKeyboardButton(label, callback_data=f"depmethod_{key}_{amount:.2f}")]
        for key, label in _available_methods()
    ]
    rows.append([InlineKeyboardButton("\U0001f3e0 Main Menu", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def _create_invoice(uid, amount, method="bep20"):
    """Cancel older pending invoices and create a fresh one. Returns (id, unique_amount)."""
    # Cancel older pending invoices for the same user to avoid confusion.
    cur.execute(
        "UPDATE deposits SET status='cancelled' WHERE user_id=? AND status='pending'",
        (uid,)
    )

    unique_amount = round(amount + random.randint(100, 999) / 100000, 5)

    # Set created explicitly in bot time (IST). The DB default is UTC,
    # which made invoices look ~5.5h old and expire after ~30 minutes.
    cur.execute(
        "INSERT INTO deposits(user_id, amount, unique_amount, status, created, method) VALUES(?,?,?,?,?,?)",
        (uid, amount, unique_amount, "pending", now_text(), method),
    )
    conn.commit()

    cur.execute("SELECT last_insert_rowid()")
    invoice_id = cur.fetchone()[0]
    return invoice_id, unique_amount


def _invoice_text(amount, unique_amount, invoice_id):
    return f"""🪙 <b>USDT BEP20</b>
<b>Auto-verification enabled</b>

⚛️ Order: <b>${amount:.2f} USDT</b>

🟢 Send EXACTLY: <code>{unique_amount:.5f}</code> <b>USDT</b>
<b>To Address (BEP20 (BNB Smart Chain)):</b>
<code>{USDT_BEP20_ADDRESS}</code>

⚠️ Send <b>exactly</b> this amount on the <b>BEP20 (BNB Smart Chain)</b> network — it must match to the <b>last decimal</b>, or your payment will NOT be matched automatically.
Expires in <b>{DEPOSIT_EXPIRE_HOURS} hours</b>.

Payment ID: <b>#{invoice_id}</b>
Payment will be verified and credited automatically."""


def _trc20_invoice_text(amount, unique_amount, invoice_id):
    return f"""\U0001fa99 <b>USDT TRC20</b>
<b>Auto-verification enabled</b>

\u269b\ufe0f Order: <b>${amount:.2f} USDT</b>

\U0001f7e2 Send EXACTLY: <code>{unique_amount:.5f}</code> <b>USDT</b>
<b>To Address (TRC20 (Tron Network)):</b>
<code>{USDT_TRC20_ADDRESS}</code>

Works with <b>Trust Wallet</b>, Binance withdrawal, or any TRC20 wallet.

\u26a0\ufe0f Send <b>exactly</b> this amount on the <b>TRC20 (Tron)</b> network \u2014 it must match to the <b>last decimal</b>, or your payment will NOT be matched automatically.
Expires in <b>{DEPOSIT_EXPIRE_HOURS} hours</b>.

Payment ID: <b>#{invoice_id}</b>
Payment will be verified and credited automatically."""


def _invoice_text_for(method, amount, unique_amount, invoice_id):
    if method == "trc20":
        return _trc20_invoice_text(amount, unique_amount, invoice_id)
    return _invoice_text(amount, unique_amount, invoice_id)


def _invoice_menu_for(method, invoice_id):
    return deposit_invoice_menu(invoice_id)


async def create_deposit_invoice(q, uid, method, amount):
    """Create an invoice for the method chosen from the payment chooser."""
    valid = {key for key, _ in _available_methods()}
    if method not in valid:
        method = "bep20"
    try:
        amount = round(float(amount), 2)
    except Exception:
        amount = 0.0
    amount = max(0.1, min(5000.0, amount))
    invoice_id, unique_amount = _create_invoice(uid, amount, method)
    await edit_or_send(
        q,
        _invoice_text_for(method, amount, unique_amount, invoice_id),
        _invoice_menu_for(method, invoice_id),
    )


async def deposit_shortfall(q, uid, amount):
    """Instantly create an invoice for a pre-computed shortfall amount."""
    try:
        amount = round(float(amount), 2)
    except Exception:
        amount = 0.0

    amount = max(0.1, min(5000.0, amount))
    methods = _available_methods()
    if len(methods) > 1:
        await edit_or_send(q, _method_chooser_text(amount), _method_menu(amount))
        return
    method = methods[0][0] if methods else "bep20"
    invoice_id, unique_amount = _create_invoice(uid, amount, method)
    await edit_or_send(q, _invoice_text_for(method, amount, unique_amount, invoice_id), _invoice_menu_for(method, invoice_id))


async def handle_deposit_amount(update, uid, text):
    try:
        amount = float(text)

        if amount < 0.1 or amount > 5000:
            await update.message.reply_text(
                "❌ <b>Invalid Amount</b>\n\nEnter amount between <code>0.1</code> and <code>5000</code> USDT.",
                parse_mode="HTML"
            )
            return True

        methods = _available_methods()
        if len(methods) > 1:
            await update.message.reply_text(
                _method_chooser_text(amount),
                reply_markup=_method_menu(amount),
                parse_mode="HTML",
            )
        else:
            method = methods[0][0] if methods else "bep20"
            invoice_id, unique_amount = _create_invoice(uid, amount, method)
            await update.message.reply_text(
                _invoice_text_for(method, amount, unique_amount, invoice_id),
                reply_markup=_invoice_menu_for(method, invoice_id),
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
