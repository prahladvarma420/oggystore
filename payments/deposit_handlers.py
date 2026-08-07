import random
import secrets
import string
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
    BINANCE_API_KEY,
    BINANCE_API_SECRET,
    BINANCE_PAY_ID,
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
                f"🎯 Exact Pay: <code>{unique_amount:.6f}</code> USDT\n"
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
    """Payment methods that are configured via env vars. BEP20 is the default.

    Labels come from the editable message templates (btn_bep20 / btn_trc20 /
    btn_binance) and may contain premium-emoji <tg-emoji> tags. Buttons are
    built with _method_button() so the FIRST premium emoji becomes the button
    icon -- exactly how the customizable main-menu buttons behave.
    """
    from database.db import get_message_template, BTN_BEP20_DEFAULT, BTN_TRC20_DEFAULT, BTN_BINANCE_DEFAULT

    def _label(key, default):
        return (get_message_template(key, default) or "").strip() or default

    methods = []
    if isinstance(USDT_BEP20_ADDRESS, str) and USDT_BEP20_ADDRESS.startswith("0x"):
        methods.append(("bep20", _label("btn_bep20", BTN_BEP20_DEFAULT)))
    if isinstance(USDT_TRC20_ADDRESS, str) and USDT_TRC20_ADDRESS.startswith("T"):
        methods.append(("trc20", _label("btn_trc20", BTN_TRC20_DEFAULT)))
    if BINANCE_API_KEY and BINANCE_API_SECRET and BINANCE_PAY_ID:
        methods.append(("binance", _label("btn_binance", BTN_BINANCE_DEFAULT)))
    return methods


def _method_chooser_text(amount):
    return (
        "\U0001f4b3 <b>Choose Payment Method</b>\n\n"
        f"\U0001f4b5 Amount: <b>{amount:.2f} USDT</b>\n\n"
        "Select how you want to pay \U0001f447"
    )


def _method_button(label, callback_data):
    """Payment-method button: the first premium emoji in the label becomes
    the button icon; the rest renders as plain text (Telegram buttons cannot
    show premium emoji inline, only as the icon)."""
    from utils.premium_emoji import button_premium, strip_premium
    text, icon = button_premium(label)
    return InlineKeyboardButton(text or strip_premium(label), callback_data=callback_data, icon_custom_emoji_id=icon)


def _method_menu(amount):
    rows = [
        [_method_button(label, f"depmethod_{key}_{amount:.2f}")]
        for key, label in _available_methods()
    ]
    rows.append([InlineKeyboardButton("\U0001f3e0 Main Menu", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def _gen_note_code(length=8):
    """Unique 8-char uppercase alphanumeric code for Binance Pay note matching."""
    alphabet = string.ascii_uppercase + string.digits
    code = "".join(secrets.choice(alphabet) for _ in range(length))
    for _ in range(20):
        cur.execute(
            # Check ALL recent invoices, not just pending ones. A code from an
            # already-paid invoice could be reissued and then match an old
            # transaction inside the Binance 2-day scan window.
            "SELECT 1 FROM deposits WHERE note_code=?", (code,)
        )
        if cur.fetchone() is None:
            return code
        code = "".join(secrets.choice(alphabet) for _ in range(length))
    return code


def _create_invoice(uid, amount, method="bep20"):
    """Cancel older pending invoices and create a fresh one.
    Returns (invoice_id, unique_amount, note_code)."""
    # Cancel older pending TOP-UP invoices only.
    # This used to cancel every pending invoice for the user, including a
    # direct product-checkout invoice they may have already paid but not yet
    # been credited for -- silently orphaning a real payment.
    cur.execute(
        "UPDATE deposits SET status='cancelled' "
        "WHERE user_id=? AND status='pending' AND checkout_product_id IS NULL",
        (uid,)
    )

    # Widen the suffix space and verify it is not already in flight. The old
    # randint(100,999)/100000 gave only 900 variants, so two users depositing
    # the same base amount collided often -- and the checker credited whichever
    # pending row it found first (i.e. potentially the WRONG user).
    unique_amount = round(amount + random.randint(1000, 9999) / 1000000, 6)
    for _ in range(25):
        cur.execute(
            "SELECT 1 FROM deposits WHERE unique_amount=? AND status='pending'",
            (unique_amount,),
        )
        if cur.fetchone() is None:
            break
        unique_amount = round(amount + random.randint(1000, 9999) / 1000000, 6)
    note_code = _gen_note_code() if method == "binance" else None

    # Set created explicitly in bot time (IST). The DB default is UTC,
    # which made invoices look ~5.5h old and expire after ~30 minutes.
    cur.execute(
        "INSERT INTO deposits(user_id, amount, unique_amount, status, created, method, note_code) VALUES(?,?,?,?,?,?,?)",
        (uid, amount, unique_amount, "pending", now_text(), method, note_code),
    )
    conn.commit()

    cur.execute("SELECT last_insert_rowid()")
    invoice_id = cur.fetchone()[0]
    return invoice_id, unique_amount, note_code


def _invoice_text(amount, unique_amount, invoice_id):
    return f"""🪙 <b>USDT BEP20</b>
<b>Auto-verification enabled</b>

⚛️ Order: <b>${amount:.2f} USDT</b>

🟢 Send EXACTLY: <code>{unique_amount:.6f}</code> <b>USDT</b>
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

\U0001f7e2 Send EXACTLY: <code>{unique_amount:.6f}</code> <b>USDT</b>
<b>To Address (TRC20 (Tron Network)):</b>
<code>{USDT_TRC20_ADDRESS}</code>

Works with <b>Trust Wallet</b>, Binance withdrawal, or any TRC20 wallet.

\u26a0\ufe0f Send <b>exactly</b> this amount on the <b>TRC20 (Tron)</b> network \u2014 it must match to the <b>last decimal</b>, or your payment will NOT be matched automatically.
Expires in <b>{DEPOSIT_EXPIRE_HOURS} hours</b>.

Payment ID: <b>#{invoice_id}</b>
Payment will be verified and credited automatically."""


def _binance_invoice_text(amount, note_code, invoice_id):
    return f"""\U0001f7e8 <b>USDT \u2014 Binance Pay</b>
<b>Auto-verification enabled</b>

\u269b\ufe0f Order: <b>${amount:.2f} USDT</b>

<b>How to pay:</b>
1\ufe0f\u20e3 Open <b>Binance App</b> \u2192 <b>Pay</b> \u2192 <b>Send</b>.
2\ufe0f\u20e3 Send <b>{amount:.2f} USDT</b> to Binance ID: <code>{BINANCE_PAY_ID}</code>
3\ufe0f\u20e3 In the <b>Note / Remark</b> field, enter EXACTLY this code:
<code>{note_code}</code>

\u26a0\ufe0f Pay in <b>USDT only</b> \u2014 any other coin will <b>NOT</b> be credited.
\u26a0\ufe0f You must include the note code above, or your payment cannot be matched.
Expires in <b>{DEPOSIT_EXPIRE_HOURS} hours</b>.

Payment ID: <b>#{invoice_id}</b>
Payment will be verified and credited automatically."""


def _invoice_text_for(method, amount, unique_amount, invoice_id, note_code=None):
    if method == "trc20":
        return _trc20_invoice_text(amount, unique_amount, invoice_id)
    if method == "binance":
        return _binance_invoice_text(amount, note_code, invoice_id)
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
    invoice_id, unique_amount, note_code = _create_invoice(uid, amount, method)
    await edit_or_send(
        q,
        _invoice_text_for(method, amount, unique_amount, invoice_id, note_code),
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
    invoice_id, unique_amount, note_code = _create_invoice(uid, amount, method)
    await edit_or_send(q, _invoice_text_for(method, amount, unique_amount, invoice_id, note_code), _invoice_menu_for(method, invoice_id))


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
            invoice_id, unique_amount, note_code = _create_invoice(uid, amount, method)
            await update.message.reply_text(
                _invoice_text_for(method, amount, unique_amount, invoice_id, note_code),
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
