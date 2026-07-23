from pathlib import Path
import html
import re
import time
from telegram.ext import ContextTypes
from database.db import get_setting, set_user_coupon, render_message_template, cur, conn, ensure_user, get_balance, is_blocked, is_admin, create_ticket, get_user_tickets, add_referral, get_referral_stats, create_withdrawal_request, get_user_withdrawals, get_user_coupon, validate_coupon, clear_user_coupon, restore_backup_zip, add_restock_notification
from utils.crypto import decrypt_text
from utils.premium_emoji import strip_premium
from keyboards.menus import main_menu, profile_menu, back_home, support_menu
from utils.texts import welcome_text
from utils.ui import edit_or_send
from utils.states import user_state
from products.product_handlers import show_shop, show_product, buy_product, show_order_preview, start_direct_checkout, show_quantity_menu, show_bundles, show_bundle_details, buy_bundle
from payments.deposit_handlers import start_deposit, handle_deposit_amount, deposit_shortfall, create_deposit_invoice
from admin.admin_handlers import show_admin_panel, handle_admin_callback, handle_admin_message
from config import ADMIN_USERNAME, ADMIN_ID
from database.db import create_gift_code, redeem_gift_code, get_user_gift_codes, get_warranty_orders, get_order_for_warranty, is_warranty_banned, count_recent_replacements, create_replacement, reserve_stock_item


# Security anti-spam settings.
RATE_LIMIT_SECONDS = 1.5
SPAM_WARN_LIMIT = 6
SPAM_LOCK_SECONDS = 30

user_last_action = {}
user_warn_count = {}
user_locked_until = {}

# ---------------- Force-join gate ----------------
# Users must join the official channels before using the bot.
# Controlled by settings: force_join_enabled ('1'/'0') and
# force_join_channels (comma-separated @usernames).
# NOTE: the bot must be an ADMIN in each channel/group to check membership.

_join_cache = {}  # uid -> cache expiry timestamp (positive results only)


def _force_join_channels():
    raw = get_setting("force_join_channels", "@oggypremiumstore,@oggypremiumchat") or ""
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        if part.startswith("https://t.me/"):
            part = "@" + part.split("t.me/")[1].strip("/")
        if part and not part.startswith("@"):
            part = "@" + part
        if part and part != "@":
            out.append(part)
    return out


async def get_unjoined_channels(bot, uid):
    """Return the list of required channels this user has NOT joined."""
    if str(get_setting("force_join_enabled", "1")).strip() != "1":
        return []
    if bot is None:
        return []
    now_ts = time.time()
    if _join_cache.get(uid, 0) > now_ts:
        return []

    missing = []
    for ch in _force_join_channels():
        try:
            member = await bot.get_chat_member(chat_id=ch, user_id=uid)
            if getattr(member, "status", "") in ("left", "kicked"):
                missing.append(ch)
        except Exception:
            # Bot not admin in that channel / wrong username -> don't lock
            # everyone out. Fix the channel setup and the gate resumes.
            continue

    if not missing:
        _join_cache[uid] = now_ts + 30  # re-check at most every 30 sec
    return missing


def force_join_text():
    return (
        "🔐 <b>Join Our Channels To Continue</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "To use this bot, please join our official channels first:\n\n"
        "📢 <b>Announcements</b> — offers, new stock &amp; updates\n"
        "💬 <b>Discussion</b> — community &amp; support\n\n"
        "1️⃣ Tap the buttons below to join\n"
        "2️⃣ Then tap <b>✅ I Joined</b>"
    )


def force_join_markup(missing):
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
    rows = []
    for ch in missing:
        uname = ch.lstrip("@")
        rows.append([InlineKeyboardButton(f"➕ Join @{uname}", url=f"https://t.me/{uname}")])
    rows.append([InlineKeyboardButton("✅ I Joined", callback_data="check_join")])
    return InlineKeyboardMarkup(rows)


async def process_warranty_request(q, uid, order_id, context):
    """Hybrid warranty: instant auto-replace for users in good standing,
    manual admin review when abuse signals appear or stock is empty."""
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton

    if str(get_setting("warranty_enabled", "1")).strip() != "1":
        await edit_or_send(q, "🛡 <b>Warranty is currently disabled.</b>", back_home())
        return

    order = get_order_for_warranty(order_id, uid)

    if not order:
        await edit_or_send(
            q,
            "❌ <b>Not Eligible</b>\n\nThis order is outside the warranty window, was already replaced once, or does not support auto-replacement.\n\nPlease create a support ticket instead.",
            back_home()
        )
        return

    _oid, w_product_id, w_product_name, w_delivery_enc = order
    old_delivery = decrypt_text(w_delivery_enc) or ""
    safe_name = html.escape(strip_premium(str(w_product_name)), quote=False)

    max_week = int(float(get_setting("warranty_max_week", "3") or 3))
    suspicious = is_warranty_banned(uid) or count_recent_replacements(uid, 7) >= max_week

    if not suspicious:
        new_code = reserve_stock_item(w_product_id, uid)

        if new_code:
            cur.execute("UPDATE products SET stock=stock-1 WHERE id=? AND stock > 0", (w_product_id,))
            conn.commit()
            rid = create_replacement(order_id, uid, w_product_id, old_delivery, new_code, "auto")

            await edit_or_send(
                q,
                "✅ <b>Replacement Sent</b>\n\n"
                f"🧾 Order: <code>#{order_id}</code>\n"
                f"📦 {safe_name}\n\n"
                f"🔑 <b>New Delivery:</b>\n<code>{html.escape(new_code, quote=False)}</code>\n\n"
                "This order's one-time warranty replacement has now been used.",
                main_menu(uid)
            )

            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "🛡 <b>Auto Replacement Issued</b>\n\n"
                        f"🧾 Replacement: <code>#{rid}</code> | Order: <code>#{order_id}</code>\n"
                        f"👤 User: <code>{uid}</code>\n"
                        f"📦 {safe_name}\n\n"
                        f"🔴 <b>Old:</b>\n<code>{html.escape(old_delivery[:600], quote=False)}</code>\n\n"
                        f"🟢 <b>New:</b>\n<code>{html.escape(new_code, quote=False)}</code>\n\n"
                        "Verify the old code if you suspect abuse — Admin Panel → 🛡 Warranty Requests."
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass

            return

    # Manual review path (abuse signals, warranty ban, or no stock left).
    rid = create_replacement(order_id, uid, w_product_id, old_delivery, "", "pending")

    await edit_or_send(
        q,
        "🕒 <b>Replacement Request Sent</b>\n\n"
        f"🧾 Order: <code>#{order_id}</code>\n"
        f"📦 {safe_name}\n\n"
        "Your request needs admin review. You will be notified here once it is processed.",
        main_menu(uid)
    )

    try:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Approve", callback_data="admin_warr_ok_" + str(rid)),
             InlineKeyboardButton("❌ Deny", callback_data="admin_warr_no_" + str(rid))],
            [InlineKeyboardButton("🚫 Warranty-Ban User", callback_data="admin_warr_ban_" + str(uid))],
        ])
        banned_note = " | 🚫 <b>warranty-banned</b>" if is_warranty_banned(uid) else ""
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "🛡 <b>Replacement Needs Review</b>\n\n"
                f"🧾 Replacement: <code>#{rid}</code> | Order: <code>#{order_id}</code>\n"
                f"👤 User: <code>{uid}</code>\n"
                f"📦 {safe_name}\n"
                f"📊 Replacements (7d): <code>{count_recent_replacements(uid, 7)}</code>" + banned_note + "\n\n"
                f"🔴 <b>Old Delivery:</b>\n<code>{html.escape(old_delivery[:600], quote=False)}</code>"
            ),
            parse_mode="HTML",
            reply_markup=kb
        )
    except Exception:
        pass


PROFILE_MSG_DEFAULT = (
    "👤 <b>My Profile</b>\n\n🆔 <b>User ID:</b> <code>{user_id}</code>\n👤 <b>Name:</b> {name}\n🔗 <b>Username:</b> {username}\n\n💼 <b>Wallet Balance:</b> <b>{balance} USDT</b>\n🛒 <b>Total Orders:</b> <b>{orders}</b>\n💰 <b>Total Spent:</b> <b>${spent}</b>\n\n💳 <b>Paid Deposits:</b> <b>{deposits}</b>\n📥 <b>Total Deposited:</b> <b>{deposited} USDT</b>\n\n🎁 <b>Referral Earnings:</b> <b>{referral_earnings} USDT</b>\n⏳ <b>Pending Referral:</b> <b>{referral_pending} USDT</b>\n\n🎟 <b>Coupon:</b> {coupon}\n🔔 <b>Notifications:</b> {notifications}"
)
WALLET_MSG_DEFAULT = (
    "💼 <b>My Wallet</b>\n\n🆔 <b>User ID:</b> <code>{user_id}</code>\n👤 <b>Name:</b> {name}\n🔗 <b>Username:</b> {username}\n\n💰 <b>Available Balance:</b> <b>{balance} USDT</b>\n📥 <b>Total Deposited:</b> <b>{deposited} USDT</b>\n🧾 <b>Paid Deposits:</b> <b>{deposits}</b>\n\n🛒 <b>Total Orders:</b> <b>{orders}</b>\n💸 <b>Total Spent:</b> <b>{spent} USDT</b>\n\n🎁 <b>Referral Earnings:</b> <b>{referral_earnings} USDT</b>\n⏳ <b>Pending Referral:</b> <b>{referral_pending} USDT</b>\n⭐ <b>Loyalty Points:</b> <b>{loyalty_points}</b>\n🏆 <b>VIP Rank:</b> <b>{vip_rank}</b>\n\nUse Deposit to add balance, then pay directly from wallet during checkout."
)
SUPPORT_PROMPT_DEFAULT = (
    "🎫 <b>Create Support Ticket</b>\n\nSend your issue/message now.\n\nExample:\nPayment done but balance not added."
)


def is_rate_limited(user_id: int) -> bool:
    if is_admin(user_id):
        return False

    now = time.time()
    locked_until = user_locked_until.get(user_id, 0)

    if locked_until and now < locked_until:
        return True

    last = user_last_action.get(user_id, 0)

    if now - last < RATE_LIMIT_SECONDS:
        user_warn_count[user_id] = user_warn_count.get(user_id, 0) + 1

        if user_warn_count[user_id] >= SPAM_WARN_LIMIT:
            user_locked_until[user_id] = now + SPAM_LOCK_SECONDS
            user_warn_count[user_id] = 0

        return True

    user_last_action[user_id] = now
    user_warn_count[user_id] = 0
    return False


def get_lock_seconds(user_id: int) -> int:
    now = time.time()
    locked_until = user_locked_until.get(user_id, 0)
    if locked_until and now < locked_until:
        return int(locked_until - now)
    return 0


async def send_rate_limit_message(obj):
    text = f"⏳ <b>Slow Down</b>\n\nPlease wait {get_lock_seconds(user.id) or 2} seconds before using the bot again."
    try:
        await edit_or_send(obj, text)
    except Exception:
        await obj.message.reply_text(text, parse_mode="HTML")


async def start(update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or update.message.from_user is None:
        return
    user = update.message.from_user
    ensure_user(user)

    try:
        welcome_coupon = get_setting("auto_welcome_coupon", "")
        if welcome_coupon:
            cur.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (uid,))
            order_count = cur.fetchone()[0]
            if order_count == 0:
                set_user_coupon(uid, welcome_coupon)
    except Exception:
        pass

    try:
        if context.args:
            ref_arg = context.args[0]
            if ref_arg.startswith("ref_"):
                referrer_id = int(ref_arg.replace("ref_", ""))
                if add_referral(referrer_id, user.id):
                    try:
                        await context.bot.send_message(
                            chat_id=referrer_id,
                            text=(
                                "🎁 <b>New Referral Joined</b>\n\n"
                                f"👤 User: <code>{user.id}</code>\n"
                                f"🧾 Name: {user.first_name or 'User'}"
                            ),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
    except Exception:
        pass

    if is_blocked(user.id):
        await update.message.reply_text(
            "🚫 <b>Access Denied</b>\n\nYour account is blocked from using this bot.",
            parse_mode="HTML"
        )
        return

    if is_rate_limited(user.id):
        await update.message.reply_text(
            f"⏳ <b>Slow Down</b>\n\nPlease wait {get_lock_seconds(user.id) or 2} seconds before using the bot again.",
            parse_mode="HTML"
        )
        return

    if not is_admin(user.id):
        missing = await get_unjoined_channels(context.bot, user.id)
        if missing:
            await update.message.reply_text(
                force_join_text(),
                reply_markup=force_join_markup(missing),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return

    await update.message.reply_text(
        welcome_text(user.first_name),
        reply_markup=main_menu(user.id),
        disable_web_page_preview=True,
        parse_mode="HTML",
    )


async def admin(update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or update.message.from_user is None:
        return
    user = update.message.from_user
    ensure_user(user)

    try:
        welcome_coupon = get_setting("auto_welcome_coupon", "")
        if welcome_coupon:
            cur.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (user.id,))
            order_count = cur.fetchone()[0]
            if order_count == 0:
                set_user_coupon(user.id, welcome_coupon)
    except Exception:
        pass

    if user.id is None:
        return

    if not is_admin(user.id):
        await update.message.reply_text("❌ <b>Admin only.</b>", parse_mode="HTML")
        return

    from keyboards.menus import admin_menu
    await update.message.reply_text(
        "👑 <b>Admin Panel</b>\n\n<i>Manage your shop from here.</i>",
        reply_markup=admin_menu(),
        parse_mode="HTML"
    )


async def buttons(update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query

    try:
        await q.answer()
    except Exception:
        pass

    user = q.from_user
    uid = user.id
    ensure_user(user)
    data = q.data or ""

    # Basic callback safety: ignore malformed/oversized callback data.
    if len(data) > 80:
        await edit_or_send(q, "❌ <b>Invalid request.</b>", back_home())
        return

    if is_blocked(uid) and not data.startswith("admin_"):
        await edit_or_send(
            q,
            "🚫 <b>Access Denied</b>\n\nYour account is blocked from using this bot.",
            back_home()
        )
        return

    if is_rate_limited(uid) and not data.startswith("admin_"):
        await edit_or_send(
            q,
            f"⏳ <b>Slow Down</b>\n\nPlease wait {get_lock_seconds(uid) or 2} seconds before clicking again.",
            back_home()
        )
        return

    try:
        _gate_bot = q.message.get_bot()
    except Exception:
        _gate_bot = None

    if data == "check_join":
        _join_cache.pop(uid, None)
        missing = await get_unjoined_channels(_gate_bot, uid)
        if missing:
            try:
                await q.answer("❌ You haven't joined all channels yet. Join them, then tap again.", show_alert=True)
            except Exception:
                pass
            await edit_or_send(q, force_join_text(), force_join_markup(missing))
            return
        try:
            await q.answer("✅ Thank you for joining!")
        except Exception:
            pass
        await edit_or_send(q, welcome_text(user.first_name), main_menu(uid))
        return

    if not is_admin(uid) and not data.startswith("admin_"):
        missing = await get_unjoined_channels(_gate_bot, uid)
        if missing:
            await edit_or_send(q, force_join_text(), force_join_markup(missing))
            return

    if data == "home":
        await edit_or_send(q, welcome_text(user.first_name), main_menu(uid))
        return

    if data == "shop":
        await show_shop(q)
        return

    if data.startswith("product_"):
        await show_product(q, int(data.split("_")[1]))
        return

    if data.startswith("qtymenu_"):
        pid = int(data.split("_")[1])
        await show_quantity_menu(q, uid, pid)
        return

    if data.startswith("previewbuy_"):
        parts = data.split("_")
        pid = int(parts[1])
        qty = int(parts[2])
        await show_order_preview(q, uid, pid, qty)
        return

    if data.startswith("customqty_"):
        pid = int(data.split("_")[1])
        user_state[uid] = f"custom_buy_qty:{pid}"
        await edit_or_send(
            q,
            """✨ <b>Custom Quantity</b>

Send the quantity you want to buy.

Example:
<code>13</code>""",
            back_home()
        )
        return

    if data.startswith("directpay_"):
        parts = data.split("_")
        pid = int(parts[1])
        qty = int(parts[2]) if len(parts) > 2 else 1
        await start_direct_checkout(q, uid, pid, qty)
        return

    if data.startswith("buy_"):
        parts = data.split("_")
        pid = int(parts[1])
        qty = int(parts[2]) if len(parts) > 2 else 1
        await buy_product(q, uid, pid, qty)
        return

    if data == "deposit":
        await start_deposit(q, uid)
        return

    if data.startswith("depneed_"):
        try:
            need_amount = float(data.split("_", 1)[1])
        except Exception:
            need_amount = 0.0
        await deposit_shortfall(q, uid, need_amount)
        return

    if data.startswith("depmethod_"):
        try:
            _, method, amt = data.split("_", 2)
            amount = float(amt)
        except Exception:
            method, amount = "bep20", 0.0
        await create_deposit_invoice(q, uid, method, amount)
        return

    if data.startswith("canceldep_"):
        try:
            invoice_id = int(data.split("_", 1)[1])
        except Exception:
            invoice_id = 0
        cur.execute(
            "UPDATE deposits SET status='cancelled' WHERE id=? AND user_id=? AND status='pending'",
            (invoice_id, uid)
        )
        conn.commit()
        await edit_or_send(
            q,
            f"🚫 <b>Payment Cancelled</b>\n\nInvoice <b>#{invoice_id}</b> has been cancelled.\nNo payment will be matched for it anymore.\n\nYou can create a new deposit anytime from 💰 Deposit.",
            main_menu(uid)
        )
        return

    if data == "bundles":
        await show_bundles(q)
        return

    if data.startswith("buybundle_"):
        await buy_bundle(q, uid, int(data.split("_")[1]))
        return

    if data.startswith("bundle_"):
        await show_bundle_details(q, uid, int(data.split("_")[1]))
        return

    if data == "deposit_history":
        from payments.deposit_handlers import show_deposit_history
        await show_deposit_history(q, uid)
        return

    if data == "wallet":
        cur.execute(
            "SELECT first_name, username, balance, referral_earnings, referral_pending, loyalty_points, vip_rank FROM users WHERE user_id=?",
            (uid,)
        )
        row = cur.fetchone()
        if row:
            first_name, username, balance, ref_earn, ref_pending, loyalty_points, vip_rank = row
        else:
            first_name, username, balance, ref_earn, ref_pending, loyalty_points, vip_rank = (user.first_name or "User", None, 0, 0, 0, 0, "Bronze")

        cur.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM deposits WHERE user_id=? AND status='paid'", (uid,))
        dep_count, dep_total = cur.fetchone()

        cur.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE user_id=?", (uid,))
        order_count, total_spent = cur.fetchone()

        uname = f"@{username}" if username else "No username"

        await edit_or_send(
            q,
            render_message_template(
                "wallet_message",
                WALLET_MSG_DEFAULT,
                user_id=uid,
                name=(first_name or "User"),
                username=uname,
                balance=f"{float(balance or 0):.2f}",
                deposited=f"{float(dep_total or 0):.2f}",
                deposits=dep_count,
                orders=order_count,
                spent=f"{float(total_spent or 0):.2f}",
                referral_earnings=f"{float(ref_earn or 0):.6f}",
                referral_pending=f"{float(ref_pending or 0):.6f}",
                loyalty_points=int(loyalty_points or 0),
                vip_rank=(vip_rank or "Bronze"),
            ),
            profile_menu()
        )
        return

    if data.startswith("checkout_coupon_remove_"):
        parts = data.split("_")
        pid = int(parts[3])
        qty = int(parts[4])
        clear_user_coupon(uid)
        await show_order_preview(q, uid, pid, qty)
        return

    if data.startswith("checkout_coupon_"):
        parts = data.split("_")
        pid = int(parts[2])
        qty = int(parts[3])
        user_state[uid] = f"checkout_coupon_code:{pid}:{qty}"
        await edit_or_send(
            q,
            """🎟 <b>Enter Coupon Code</b>

Send your coupon code now.

Example:
<code>WELCOME10</code>""",
            back_home()
        )
        return

    if data == "apply_coupon":
        user_state[uid] = "apply_coupon_code"
        await edit_or_send(
            q,
            """🎟 <b>Apply Coupon</b>

Send your coupon code.

Example:
<code>WELCOME10</code>""",
            back_home()
        )
        return

    if data == "remove_coupon":
        clear_user_coupon(uid)
        await edit_or_send(q, "🗑 <b>Coupon removed.</b>", main_menu(uid))
        return

    if data.startswith("notify_stock_"):
        product_id = int(data.split("_")[-1])
        add_restock_notification(uid, product_id)
        await edit_or_send(
            q,
            "🔔 <b>Restock Alert Added</b>\n\nYou will be notified when this product is back in stock.",
            back_home()
        )
        return

    if data == "gift_codes":
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        gmin = float(get_setting("gift_min_amount", "1") or 1)
        gmax = float(get_setting("gift_max_amount", "500") or 500)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Buy Gift Code", callback_data="gift_buy")],
            [InlineKeyboardButton("🔑 Redeem Gift Code", callback_data="gift_redeem")],
            [InlineKeyboardButton("📂 My Gift Codes", callback_data="gift_mine")],
            [InlineKeyboardButton("◀️ Back", callback_data="home")],
        ])
        await edit_or_send(
            q,
            "🎁 <b>Gift Codes</b>\n\n"
            "Buy a gift code with your wallet balance and share it with a friend — they redeem it and the balance is added to their wallet instantly.\n\n"
            f"💰 Amount range: <code>{gmin:.2f}</code> – <code>{gmax:.2f}</code> USDT",
            kb
        )
        return

    if data == "gift_buy":
        user_state[uid] = "gift_buy_amount"
        await edit_or_send(
            q,
            "🎁 <b>Buy Gift Code</b>\n\n"
            f"💼 Your balance: <code>{get_balance(uid):.2f} USDT</code>\n\n"
            "Send the amount for the gift code.\n\nExample:\n<code>5</code>",
            back_home()
        )
        return

    if data == "gift_redeem":
        user_state[uid] = "gift_redeem_code"
        await edit_or_send(
            q,
            "🔑 <b>Redeem Gift Code</b>\n\nSend your gift code now.\n\nExample:\n<code>GIFT-AB12CD34EF</code>",
            back_home()
        )
        return

    if data == "gift_mine":
        rows = get_user_gift_codes(uid, 10)
        text = "📂 <b>My Gift Codes</b>\n\n"
        if not rows:
            text += "You haven't created any gift codes yet."
        else:
            for code, amount, redeemed_by, created in rows:
                status = "✅ Redeemed" if redeemed_by else "🟢 Active"
                text += f"🎁 <code>{code}</code>\n💰 <code>{float(amount):.2f} USDT</code> | {status}\n\n"
        await edit_or_send(q, text, back_home())
        return

    if data == "warranty_menu":
        if str(get_setting("warranty_enabled", "1")).strip() != "1":
            await edit_or_send(q, "🛡 <b>Warranty is currently disabled.</b>", back_home())
            return

        w_hours = int(float(get_setting("warranty_hours", "24") or 24))
        w_rows = get_warranty_orders(uid)

        if not w_rows:
            await edit_or_send(
                q,
                "🛡 <b>Warranty Replacement</b>\n\n"
                f"No eligible orders found.\n\nOrders qualify for <b>one</b> free replacement within <b>{w_hours} hours</b> of purchase.\n\nFor anything else, please create a support ticket.",
                back_home()
            )
            return

        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        kb_rows = []
        for w_order_id, w_pname, _wpid, _wcreated in w_rows:
            label = "🧾 #" + str(w_order_id) + " — " + strip_premium(str(w_pname))[:30]
            kb_rows.append([InlineKeyboardButton(label, callback_data="warranty_req_" + str(w_order_id))])
        kb_rows.append([InlineKeyboardButton("◀️ Back", callback_data="home")])

        await edit_or_send(
            q,
            "🛡 <b>Warranty Replacement</b>\n\n"
            f"Got a dead code? Orders below are inside the <b>{w_hours}h</b> warranty window.\n\nSelect the order to replace (one replacement per order):",
            InlineKeyboardMarkup(kb_rows)
        )
        return

    if data.startswith("warranty_req_"):
        try:
            w_order_id = int(data.split("_")[2])
        except Exception:
            await edit_or_send(q, "❌ <b>Invalid request.</b>", back_home())
            return
        await process_warranty_request(q, uid, w_order_id, context)
        return

    if data == "search":
        user_state[uid] = "user_search_product"
        await edit_or_send(
            q,
            "🔍 <b>Search Products</b>\n\nSend a product name or keyword to search.",
            back_home()
        )
        return

    if data == "support_ticket":
        user_state[uid] = "support_ticket_message"
        await edit_or_send(
            q,
            render_message_template(
                "support_prompt",
                SUPPORT_PROMPT_DEFAULT,
                name=(q.from_user.first_name or ""),
            ),
            back_home()
        )
        return

    if data == "profile":
        cur.execute(
            "SELECT first_name, username, balance, notifications, joined, referral_earnings, referral_pending, loyalty_points, vip_rank FROM users WHERE user_id=?",
            (uid,)
        )
        row = cur.fetchone()
        if row:
            first_name, username, bal, notifications, joined, ref_earn, ref_pending, loyalty_points, vip_rank = row
        else:
            first_name, username, bal, notifications, joined, ref_earn, ref_pending, loyalty_points, vip_rank = ("User", "No username", 0, 1, "Unknown", 0, 0, 0, "Bronze")

        cur.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE user_id=?", (uid,))
        order_count, total_spent = cur.fetchone()

        cur.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM deposits WHERE user_id=? AND status='paid'", (uid,))
        dep_count, dep_total = cur.fetchone()

        uname = f"@{username}" if username and username != "No username" else "No username"

        await edit_or_send(
            q,
            render_message_template(
                "profile_message",
                PROFILE_MSG_DEFAULT,
                user_id=uid,
                name=(first_name or "User"),
                username=uname,
                balance=f"{float(bal or 0):.2f}",
                orders=order_count,
                spent=f"{float(total_spent or 0):.2f}",
                deposits=dep_count,
                deposited=f"{float(dep_total or 0):.2f}",
                referral_earnings=f"{float(ref_earn or 0):.6f}",
                referral_pending=f"{float(ref_pending or 0):.6f}",
                coupon=(get_user_coupon(uid) or "None"),
                notifications=("ON ✅" if notifications else "OFF ❌"),
            ),
            profile_menu()
        )
        return

    if data == "my_stats":
        cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE user_id=?",
            (uid,)
        )
        total_orders, total_spent = cur.fetchone()

        await edit_or_send(
            q,
            f"""📊 <b>My Stats</b>

📦 <b>Total Orders:</b> {total_orders}
💸 <b>Total Spent:</b> <code>{total_spent:.2f} USDT</code>
💰 <b>Current Balance:</b> <code>{get_balance(uid):.2f} USDT</code>

Thank you for using our store.""",
            profile_menu()
        )
        return

    if data == "toggle_notifications":
        cur.execute(
            "UPDATE users SET notifications = CASE notifications WHEN 1 THEN 0 ELSE 1 END WHERE user_id=?",
            (uid,)
        )
        conn.commit()

        await edit_or_send(
            q,
            "🔔 <b>Notification setting updated.</b>\n\nYour preference has been saved.",
            profile_menu()
        )
        return

    if data == "orders":
        cur.execute(
            "SELECT id, product_name, price, delivery, created FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (uid,)
        )
        rows = cur.fetchall()

        text = "📦 <b>My Orders</b>\n\n"

        if not rows:
            text += "No orders found yet."
        else:
            shown = 0
            for order_id, name, price, delivery, created in rows:
                name = html.escape(strip_premium(str(name)), quote=False)
                delivery = html.escape(str(decrypt_text(delivery) or ""), quote=False)
                if len(delivery) > 700:
                    delivery = delivery[:700] + "..."
                entry = (
                    f"🧾 <b>Order #{order_id}</b>\n"
                    f"📦 {name}\n"
                    f"💰 <code>{float(price or 0):.2f} USDT</code>\n"
                    f"🔑 <b>Delivery:</b>\n<code>{delivery}</code>\n\n"
                )
                if len(text) + len(entry) > 3800:
                    text += f"… and <b>{len(rows) - shown}</b> more order(s)."
                    break
                text += entry
                shown += 1

        await edit_or_send(q, text, profile_menu())
        return

    if data == "referrals":
        ref_count, total_earned, pending = get_referral_stats(uid)
        bot_username = (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start=ref_{uid}"
        percent = float(get_setting("referral_percent", "5") or 5)

        await edit_or_send(
            q,
            f"""🎁 <b>Referral Program</b>

🔗 <b>Your Referral Link:</b>
<code>{ref_link}</code>

👥 <b>Total Referrals:</b> <code>{ref_count}</code>
💰 <b>Total Earned:</b> <code>{total_earned:.6f} USDT</code>
💸 <b>Available To Withdraw:</b> <code>{pending:.6f} USDT</code>
📊 <b>Commission:</b> <code>{percent:.2f}%</code> of referred purchases

Share your link and earn automatically when your referrals buy products.""",
            profile_menu()
        )
        return

    if data == "withdraw_referral":
        ref_count, total_earned, pending = get_referral_stats(uid)
        minimum = float(get_setting("min_withdrawal", "1") or 1)

        if pending < minimum:
            await edit_or_send(
                q,
                f"""💸 <b>Withdraw Referral Earnings</b>

❌ Minimum withdrawal not reached.

💰 <b>Available:</b> <code>{pending:.6f} USDT</code>
📌 <b>Minimum:</b> <code>{minimum:.6f} USDT</code>""",
                profile_menu()
            )
            return

        user_state[uid] = "withdraw_referral"
        await edit_or_send(
            q,
            f"""💸 <b>Withdraw Referral Earnings</b>

💰 <b>Available:</b> <code>{pending:.6f} USDT</code>

Send in this format:
<code>AMOUNT | WALLET_ADDRESS</code>

Example:
<code>1.5 | 0xYourBEP20Wallet</code>""",
            profile_menu()
        )
        return

    if data == "withdraw_history":
        rows = get_user_withdrawals(uid, 10)
        text = "📂 <b>Withdrawal History</b>\\n\\n"
        if not rows:
            text += "No withdrawal requests yet."
        else:
            for wid, amount, wallet, status, note, created, updated in rows:
                icon = "🟡" if status == "pending" else "🟢" if status == "approved" else "🔴"
                text += (
                    f"{icon} <b>Request #{wid}</b>\\n"
                    f"💰 Amount: <code>{amount:.6f} USDT</code>\\n"
                    f"📍 Status: <b>{status}</b>\\n"
                    f"👛 Wallet: <code>{wallet}</code>\\n"
                )
                if note:
                    text += f"📝 Note: {note}\\n"
                text += f"🕒 <code>{created}</code>\\n\\n"

        if len(text) > 3900:
            text = text[:3900] + "\\n..."

        await edit_or_send(q, text, profile_menu())
        return

    if data == "support":
        await edit_or_send(
            q,
            f"""🎫 <b>Support Ticket</b>

Need help with payment, order, or product?

Create a ticket and admin will reply inside the bot.

For faster help, include:
• Order ID
• Payment TXID if payment issue
• Screenshot/video proof if needed

👑 Admin: {ADMIN_USERNAME}""",
            support_menu()
        )
        return

    if data == "create_ticket":
        user_state[uid] = "create_ticket"
        await edit_or_send(
            q,
            """🎫 <b>Create Support Ticket</b>

Send your issue in this format:

<code>Subject | Your message</code>

Example:
<code>Payment issue | I paid but balance not added. TXID: ...</code>""",
            support_menu()
        )
        return

    if data == "my_tickets":
        rows = get_user_tickets(uid, 10)
        text = "📂 <b>My Tickets</b>\n\n"

        if not rows:
            text += "No tickets found."
        else:
            for ticket_id, subject, message, status, admin_reply, created, updated in rows:
                subject = html.escape(str(subject), quote=False)
                message = html.escape(str(message), quote=False)
                if admin_reply:
                    admin_reply = html.escape(str(admin_reply), quote=False)
                status_icon = "🟢 Open" if status == "open" else "✅ Answered" if status == "answered" else "🔒 Closed"
                short_msg = message[:120] + "..." if len(message) > 120 else message
                text += (
                    f"🎫 <b>Ticket #{ticket_id}</b>\n"
                    f"📌 <b>Subject:</b> {subject}\n"
                    f"📍 <b>Status:</b> {status_icon}\n"
                    f"📝 <b>Message:</b> {short_msg}\n"
                )

                if admin_reply:
                    text += f"👑 <b>Admin Reply:</b> {admin_reply}\n"

                text += "\n"


        if len(text) > 3900:
            text = text[:3900] + "\n..."

        await edit_or_send(q, text, support_menu())
        return

    if data == "clear_chat":
        await edit_or_send(
            q,
            "🧹 <b>Done.</b>\n\nUse <code>/start</code> to open the menu again.",
            back_home()
        )
        return

    if data == "admin_panel":
        await show_admin_panel(q, uid)
        return

    if data.startswith("admin_"):
        handled = await handle_admin_callback(q, uid, data, context)
        if handled:
            return


async def message_handler(update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or update.message.from_user is None:
        return
    user = update.message.from_user
    uid = user.id
    text = update.message.text.strip()

    ensure_user(user)

    try:
        welcome_coupon = get_setting("auto_welcome_coupon", "")
        if welcome_coupon:
            cur.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (uid,))
            order_count = cur.fetchone()[0]
            if order_count == 0:
                set_user_coupon(uid, welcome_coupon)
    except Exception:
        pass

    if is_blocked(uid):
        await update.message.reply_text(
            "🚫 <b>Access Denied</b>\n\nYour account is blocked from using this bot.",
            parse_mode="HTML"
        )
        return

    if is_rate_limited(uid):
        await update.message.reply_text(
            f"⏳ <b>Slow Down</b>\n\nPlease wait {get_lock_seconds(uid) or 2} seconds before sending another message.",
            parse_mode="HTML"
        )
        return

    state = user_state.get(uid)

    if state and state.startswith("m365_email:"):
        _m365_parts = state.split(":")
        try:
            m365_order_id = int(_m365_parts[1])
        except Exception:
            m365_order_id = 0
        m365_warranty = _m365_parts[2] if len(_m365_parts) > 2 else ""

        email = text.strip()

        if not re.fullmatch(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", email):
            await update.message.reply_text(
                "❌ <b>That doesn't look like a valid email.</b>\n\nPlease send only your email address.\n\nExample: <code>yourname@gmail.com</code>",
                parse_mode="HTML"
            )
            return

        user_state.pop(uid, None)

        from database.db import create_m365_invite
        from utils.crypto import encrypt_text

        invite_id = create_m365_invite(uid, m365_order_id, email, m365_warranty)

        try:
            cur.execute(
                "UPDATE orders SET delivery=? WHERE id=?",
                (encrypt_text(f"Microsoft 365 Family invite → {email}"), m365_order_id)
            )
            conn.commit()
        except Exception:
            pass

        await update.message.reply_text(
            "📨 <b>Sending your Microsoft 365 invite...</b>\n\n"
            f"📧 <b>Email:</b> <code>{html.escape(email, quote=False)}</code>\n\n"
            "⏳ This usually takes under a minute. You will get a confirmation message here.",
            parse_mode="HTML"
        )

        import asyncio
        from services.m365_family import process_invite
        asyncio.create_task(process_invite(context.bot, invite_id))
        return

    if state and state.startswith("canva_email:"):
        try:
            canva_order_id = int(state.split(":")[1])
        except Exception:
            canva_order_id = 0

        email = text.strip()

        if not re.fullmatch(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", email):
            await update.message.reply_text(
                "❌ <b>That doesn't look like a valid email.</b>\n\nPlease send only your email address.\n\nExample: <code>yourname@gmail.com</code>",
                parse_mode="HTML"
            )
            return

        user_state.pop(uid, None)

        from database.db import create_canva_invite
        from utils.crypto import encrypt_text

        invite_id = create_canva_invite(uid, canva_order_id, email)

        try:
            cur.execute(
                "UPDATE orders SET delivery=? WHERE id=?",
                (encrypt_text(f"Canva invite → {email}"), canva_order_id)
            )
            conn.commit()
        except Exception:
            pass

        await update.message.reply_text(
            "📨 <b>Sending your Canva invite...</b>\n\n"
            f"📧 <b>Email:</b> <code>{html.escape(email, quote=False)}</code>\n\n"
            "⏳ This usually takes under a minute. You will get a confirmation message here.",
            parse_mode="HTML"
        )

        import asyncio
        from services.canva_invite import process_invite
        asyncio.create_task(process_invite(context.bot, invite_id))
        return

    if state and state.startswith("checkout_coupon_code:"):
        try:
            state_parts = state.split(":")
            pid = int(state_parts[1])
            qty = int(state_parts[2])
            code = text.strip().upper()

            coupon, error = validate_coupon(code)
            if not coupon:
                await update.message.reply_text(
                    f"❌ <b>Invalid Coupon</b>\n\n{error}",
                    parse_mode="HTML"
                )
                return

            set_user_coupon(uid, code)
            user_state.pop(uid, None)

            await update.message.reply_text(
                f"""✅ <b>Coupon Applied</b>

🎟 Code: <code>{code}</code>
💝 Discount: <code>{coupon['value']}{'%' if coupon['discount_type'] == 'percent' else ' USDT'}</code>

Now open checkout again to see the updated final price.""",
                parse_mode="HTML"
            )

            class MessageQuery:
                def __init__(self, message, from_user):
                    self.message = message
                    self.from_user = from_user
                    self.data = ""

                async def edit_message_text(self, text, parse_mode=None, reply_markup=None, disable_web_page_preview=None):
                    await self.message.reply_text(
                        text,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                        disable_web_page_preview=disable_web_page_preview,
                    )

            fake_q = MessageQuery(update.message, user)
            await show_order_preview(fake_q, uid, pid, qty)
            return

        except Exception as exc:
            await update.message.reply_text(
                f"❌ <b>Could not apply coupon.</b>\n\n<code>{exc}</code>",
                parse_mode="HTML"
            )
            return

            set_user_coupon(uid, code)
            user_state.pop(uid, None)

            class MessageQuery:
                def __init__(self, message, from_user):
                    self.message = message
                    self.from_user = from_user
                    self.data = ""

                async def edit_message_text(self, text, parse_mode=None, reply_markup=None, disable_web_page_preview=None):
                    await self.message.reply_text(
                        text,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                        disable_web_page_preview=disable_web_page_preview,
                    )

            fake_q = MessageQuery(update.message, user)
            await show_order_preview(fake_q, uid, pid, qty)
            return

        except Exception:
            await update.message.reply_text(
                "❌ <b>Could not apply coupon.</b>",
                parse_mode="HTML"
            )
            return

    if state == "apply_coupon_code":
        try:
            code = text.strip().upper()
            coupon, error = validate_coupon(code)

            if not coupon:
                await update.message.reply_text(
                    f"❌ <b>Invalid Coupon</b>\n\n{error}",
                    parse_mode="HTML"
                )
                return

            set_user_coupon(uid, code)
            user_state.pop(uid, None)

            dtype = "%" if coupon["discount_type"] == "percent" else "USDT"
            await update.message.reply_text(
                f"""✅ <b>Coupon Applied</b>

🎟 Code: <code>{code}</code>
💰 Discount: <code>{coupon['value']}{dtype}</code>

It will be used on your next checkout.""",
                reply_markup=main_menu(uid),
                parse_mode="HTML"
            )
            return

        except Exception as exc:
            await update.message.reply_text(
                f"❌ <b>Could not apply coupon.</b>\n\n<code>{exc}</code>",
                parse_mode="HTML"
            )
            return

        set_user_coupon(uid, code)
        user_state.pop(uid, None)

        dtype = "%" if coupon["discount_type"] == "percent" else "USDT"
        await update.message.reply_text(
            f"""✅ <b>Coupon Applied</b>

🎟 Code: <code>{code}</code>
💰 Discount: <code>{coupon['value']}{dtype}</code>

It will be used on your next wallet purchase.""",
            reply_markup=main_menu(uid),
            parse_mode="HTML"
        )
        return

    if state and state.startswith("custom_buy_qty:"):
        try:
            pid = int(state.split(":")[1])
            qty = int(text.strip())

            if qty <= 0:
                await update.message.reply_text("❌ Quantity must be more than 0.", parse_mode="HTML")
                return

            user_state.pop(uid, None)

            class MessageQuery:
                def __init__(self, message, from_user):
                    self.message = message
                    self.from_user = from_user
                    self.data = ""

                async def edit_message_text(self, text, parse_mode=None, reply_markup=None, disable_web_page_preview=None):
                    await self.message.reply_text(
                        text,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                        disable_web_page_preview=disable_web_page_preview,
                    )

            fake_q = MessageQuery(update.message, user)
            await show_order_preview(fake_q, uid, pid, qty)
            return

        except Exception:
            await update.message.reply_text(
                "❌ <b>Invalid quantity.</b>\n\nSend only a number, example: <code>13</code>",
                parse_mode="HTML"
            )
            return

    if state == "user_search_product":
        try:
            keyword = text.strip()
            if not keyword:
                await update.message.reply_text("❌ Please send a search keyword.", parse_mode="HTML")
                return

            cur.execute(
                """
                SELECT id, name, price, stock
                FROM products
                WHERE active=1 AND (name LIKE ? OR description LIKE ?)
                ORDER BY featured DESC, id DESC
                LIMIT 10
                """,
                (f"%{keyword}%", f"%{keyword}%")
            )
            rows = cur.fetchall()
            user_state.pop(uid, None)

            if not rows:
                await update.message.reply_text(
                    f"🔍 <b>No products found for:</b> <code>{html.escape(str(keyword), quote=False)}</code>",
                    reply_markup=main_menu(uid),
                    parse_mode="HTML"
                )
                return

            from telegram import InlineKeyboardMarkup
            from utils.button_styles import AutoStyledButton as InlineKeyboardButton
            from utils.premium_emoji import strip_premium, first_premium_id, button_premium
            buttons = []
            msg = f"🔍 <b>Search Results for:</b> <code>{html.escape(str(keyword), quote=False)}</code>\n\n"
            for pid, name, price, stock in rows:
                name_html = html.escape(strip_premium(str(name)), quote=False)
                msg += f"• <b>{name_html}</b> — <code>${float(price):.2f}</code> | Stock: <code>{stock}</code>\n"
                buttons.append([InlineKeyboardButton(f"🛍 {button_premium(name)[0][:35]}", callback_data=f"product_{pid}", icon_custom_emoji_id=button_premium(name)[1])])
            buttons.append([InlineKeyboardButton("◀️ Back", callback_data="home")])

            await update.message.reply_text(
                msg,
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="HTML"
            )
            return

        except Exception as exc:
            user_state.pop(uid, None)
            await update.message.reply_text(
                f"❌ <b>Search failed.</b>\n\n<code>{exc}</code>",
                reply_markup=main_menu(uid),
                parse_mode="HTML"
            )
            return

    if state == "gift_buy_amount":
        try:
            g_amount = round(float(text.strip()), 2)
        except Exception:
            await update.message.reply_text("❌ <b>Invalid amount.</b>\n\nSend only a number, example: <code>5</code>", parse_mode="HTML")
            return

        gmin = float(get_setting("gift_min_amount", "1") or 1)
        gmax = float(get_setting("gift_max_amount", "500") or 500)

        if g_amount < gmin or g_amount > gmax:
            await update.message.reply_text(
                f"❌ <b>Amount must be between {gmin:.2f} and {gmax:.2f} USDT.</b>",
                parse_mode="HTML"
            )
            return

        gift_code = create_gift_code(uid, g_amount)
        user_state.pop(uid, None)

        if not gift_code:
            await update.message.reply_text(
                f"❌ <b>Insufficient balance.</b>\n\n💼 Your balance: <code>{get_balance(uid):.2f} USDT</code>",
                reply_markup=main_menu(uid),
                parse_mode="HTML"
            )
            return

        await update.message.reply_text(
            "✅ <b>Gift Code Created</b>\n\n"
            f"🎁 <code>{gift_code}</code>\n"
            f"💰 Value: <code>{g_amount:.2f} USDT</code>\n\n"
            "Share this code with anyone — they redeem it in 👤 Profile → 🎁 Gift Codes.\n\n"
            f"💼 New balance: <code>{get_balance(uid):.2f} USDT</code>",
            reply_markup=main_menu(uid),
            parse_mode="HTML"
        )
        return

    if state == "gift_redeem_code":
        g_ok, g_result = redeem_gift_code(uid, text)
        user_state.pop(uid, None)

        if not g_ok:
            await update.message.reply_text(f"❌ <b>{g_result}</b>", reply_markup=main_menu(uid), parse_mode="HTML")
            return

        await update.message.reply_text(
            "🎉 <b>Gift Code Redeemed</b>\n\n"
            f"💰 <code>{float(g_result):.2f} USDT</code> added to your wallet.\n\n"
            f"💼 New balance: <code>{get_balance(uid):.2f} USDT</code>",
            reply_markup=main_menu(uid),
            parse_mode="HTML"
        )
        return

    if state == "deposit_amount":
        await handle_deposit_amount(update, uid, text)
        return

    if state == "withdraw_referral":
        try:
            amount_text, wallet = [x.strip() for x in text.split("|", 1)]
            amount = float(amount_text)
            minimum = float(get_setting("min_withdrawal", "1") or 1)

            if amount < minimum:
                await update.message.reply_text(
                    f"❌ <b>Minimum withdrawal is {minimum:.6f} USDT.</b>",
                    parse_mode="HTML"
                )
                return

            request_id = create_withdrawal_request(uid, amount, wallet)
            user_state.pop(uid, None)

            if not request_id:
                await update.message.reply_text(
                    "❌ <b>Withdrawal failed.</b>\\n\\nCheck your available referral balance.",
                    reply_markup=profile_menu(),
                    parse_mode="HTML"
                )
                return

            await update.message.reply_text(
                f"""✅ <b>Withdrawal Request Created</b>

🧾 <b>Request ID:</b> <code>#{request_id}</code>
💰 <b>Amount:</b> <code>{amount:.6f} USDT</code>
👛 <b>Wallet:</b> <code>{wallet}</code>

Admin will review your request.""",
                reply_markup=profile_menu(),
                parse_mode="HTML"
            )

            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "💸 <b>New Withdrawal Request</b>\\n\\n"
                        f"🧾 Request: <code>#{request_id}</code>\\n"
                        f"👤 User: <code>{uid}</code>\\n"
                        f"💰 Amount: <code>{amount:.6f} USDT</code>\\n"
                        f"👛 Wallet: <code>{wallet}</code>\\n\\n"
                        "Admin Panel → Withdrawals"
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass

            return

        except Exception:
            await update.message.reply_text(
                "❌ <b>Wrong Format</b>\\n\\nUse: <code>AMOUNT | WALLET_ADDRESS</code>",
                parse_mode="HTML"
            )
            return

    if state == "support_ticket_message":
        try:
            subject = "Support Ticket"
            message = text.strip()

            if not message:
                raise ValueError("empty")

            ticket_id = create_ticket(uid, subject, message)
            user_state.pop(uid, None)

            await update.message.reply_text(
                f"""✅ <b>Ticket Created</b>

🎫 <b>Ticket ID:</b> <code>#{ticket_id}</code>

Admin will reply soon.""",
                reply_markup=support_menu(),
                parse_mode="HTML"
            )

            try:
                from config import ADMIN_ID
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "🎫 <b>New Support Ticket</b>\n\n"
                        f"🧾 <b>Ticket:</b> <code>#{ticket_id}</code>\n"
                        f"👤 <b>User:</b> <code>{uid}</code>\n"
                        f"📌 <b>Subject:</b> {html.escape(str(subject), quote=False)}\n"
                        f"📝 <b>Message:</b> {html.escape(str(message), quote=False)}\n\n"
                        f"Reply: <code>/replyticket {ticket_id} your reply</code>"
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass

            return

        except Exception:
            await update.message.reply_text(
                "❌ <b>Please send your issue/message.</b>",
                parse_mode="HTML"
            )
            return

    if state == "create_ticket":
        try:
            subject, message = [x.strip() for x in text.split("|", 1)]

            if not subject or not message:
                raise ValueError("empty")

            ticket_id = create_ticket(uid, subject, message)
            user_state.pop(uid, None)

            await update.message.reply_text(
                f"""✅ <b>Ticket Created</b>

🎫 <b>Ticket ID:</b> <code>#{ticket_id}</code>
📌 <b>Subject:</b> {html.escape(str(subject), quote=False)}

Admin will reply soon.""",
                reply_markup=support_menu(),
                parse_mode="HTML"
            )

            try:
                from config import ADMIN_ID
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "🎫 <b>New Support Ticket</b>\n\n"
                        f"🧾 <b>Ticket:</b> <code>#{ticket_id}</code>\n"
                        f"👤 <b>User:</b> <code>{uid}</code>\n"
                        f"📌 <b>Subject:</b> {html.escape(str(subject), quote=False)}\n"
                        f"📝 <b>Message:</b> {html.escape(str(message), quote=False)}\n\n"
                        f"Reply: <code>/replyticket {ticket_id} your reply</code>"
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass

            return

        except Exception:
            await update.message.reply_text(
                "❌ <b>Wrong Format</b>\n\nUse: <code>Subject | Your message</code>",
                parse_mode="HTML"
            )
            return

    if state and state.startswith("admin_"):
        handled = await handle_admin_message(update, context, uid, text, state)
        if handled:
            return

    await update.message.reply_text(
        "ℹ️ Use <code>/start</code> to open the menu.",
        parse_mode="HTML"
    )


async def photo_handler(update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or update.message.from_user is None:
        return
    user = update.message.from_user
    uid = user.id
    ensure_user(user)

    try:
        welcome_coupon = get_setting("auto_welcome_coupon", "")
        if welcome_coupon:
            cur.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (uid,))
            order_count = cur.fetchone()[0]
            if order_count == 0:
                set_user_coupon(uid, welcome_coupon)
    except Exception:
        pass

    if not is_admin(uid):
        return

    state = user_state.get(uid)

    if state and state.startswith("checkout_coupon_code:"):
        try:
            _, pid_text, qty_text = state.split(":")
            pid = int(pid_text)
            qty = int(qty_text)
            code = text.strip().upper()

            coupon, error = validate_coupon(code)
            if not coupon:
                await update.message.reply_text(
                    f"❌ <b>Invalid Coupon</b>\n\n{error}",
                    parse_mode="HTML"
                )
                return

            set_user_coupon(uid, code)
            user_state.pop(uid, None)

            class MessageQuery:
                def __init__(self, message, from_user):
                    self.message = message
                    self.from_user = from_user
                    self.data = ""

                async def edit_message_text(self, text, parse_mode=None, reply_markup=None, disable_web_page_preview=None):
                    await self.message.reply_text(
                        text,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                        disable_web_page_preview=disable_web_page_preview,
                    )

            fake_q = MessageQuery(update.message, user)
            await show_order_preview(fake_q, uid, pid, qty)
            return

        except Exception:
            await update.message.reply_text(
                "❌ <b>Could not apply coupon.</b>",
                parse_mode="HTML"
            )
            return

    if state == "apply_coupon_code":
        code = text.strip().upper()
        coupon, error = validate_coupon(code)

        if not coupon:
            await update.message.reply_text(
                f"❌ <b>Invalid Coupon</b>\n\n{error}",
                parse_mode="HTML"
            )
            return

        set_user_coupon(uid, code)
        user_state.pop(uid, None)

        dtype = "%" if coupon["discount_type"] == "percent" else "USDT"
        await update.message.reply_text(
            f"""✅ <b>Coupon Applied</b>

🎟 Code: <code>{code}</code>
💰 Discount: <code>{coupon['value']}{dtype}</code>

It will be used on your next wallet purchase.""",
            reply_markup=main_menu(uid),
            parse_mode="HTML"
        )
        return

    if state and state.startswith("custom_buy_qty:"):
        try:
            pid = int(state.split(":")[1])
            qty = int(text.strip())

            if qty <= 0:
                await update.message.reply_text("❌ Quantity must be more than 0.", parse_mode="HTML")
                return

            user_state.pop(uid, None)

            class MessageQuery:
                def __init__(self, message, from_user):
                    self.message = message
                    self.from_user = from_user
                    self.data = ""

                async def edit_message_text(self, text, parse_mode=None, reply_markup=None, disable_web_page_preview=None):
                    await self.message.reply_text(
                        text,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                        disable_web_page_preview=disable_web_page_preview,
                    )

            fake_q = MessageQuery(update.message, user)
            await show_order_preview(fake_q, uid, pid, qty)
            return

        except Exception:
            await update.message.reply_text(
                "❌ <b>Invalid quantity.</b>\n\nSend only a number, example: <code>13</code>",
                parse_mode="HTML"
            )
            return

    if state == "admin_add_product_photo":
        try:
            admin_temp_data = None
            from admin.admin_handlers import admin_temp_data, add_product_preview_text, add_product_preview_keyboard
            file_id = update.message.photo[-1].file_id
            admin_temp_data.setdefault(uid, {})["image_file_id"] = file_id
            user_state[uid] = "admin_add_product_stock"
            await update.message.reply_text(
                "✅ <b>Photo saved.</b>\n\n🔑 Now send stock codes/accounts, one per line.",
                parse_mode="HTML"
            )
        except Exception as exc:
            await update.message.reply_text(f"❌ <b>Photo save failed.</b>\n\n<code>{exc}</code>", parse_mode="HTML")
        return

    if state and state.startswith("admin_easy_edit:") and state.endswith(":photo"):
        try:
            from database.db import get_setting, set_user_coupon,  render_message_template,  set_product_image
            product_id = int(state.split(":")[1])
            file_id = update.message.photo[-1].file_id
            set_product_image(product_id, file_id)
            user_state.pop(uid, None)
            await update.message.reply_text(
                f"✅ <b>Product photo updated.</b>\n\n🆔 Product ID: <code>{product_id}</code>",
                reply_markup=main_menu(uid),
                parse_mode="HTML"
            )
        except Exception as exc:
            await update.message.reply_text(f"❌ <b>Image save failed.</b>\n\n<code>{exc}</code>", parse_mode="HTML")
        return

    if state == "admin_wait_product_image":
        try:
            from database.db import get_setting, set_user_coupon,  render_message_template,  set_product_image
            product_id = context.user_data.get("image_product_id")
            if not product_id:
                await update.message.reply_text("❌ Product ID missing. Start again from Admin Panel.", parse_mode="HTML")
                user_state.pop(uid, None)
                return

            file_id = update.message.photo[-1].file_id
            set_product_image(int(product_id), file_id)
            user_state.pop(uid, None)
            context.user_data.pop("image_product_id", None)

            await update.message.reply_text(
                f"✅ <b>Product image saved.</b>\\n\\n🆔 Product ID: <code>{product_id}</code>",
                reply_markup=main_menu(uid),
                parse_mode="HTML"
            )
        except Exception as exc:
            await update.message.reply_text(
                f"❌ <b>Image save failed.</b>\\n\\n<code>{exc}</code>",
                parse_mode="HTML"
            )



async def document_handler(update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or update.message.from_user is None:
        return
    user = update.message.from_user
    uid = user.id
    state = user_state.get(uid)

    if state and str(state).startswith("admin_upload_m365_session") and is_admin(uid):
        _sess_warranty = "6m" if str(state).endswith(":6m") else ("1y" if str(state).endswith(":1y") else "")
        try:
            doc = update.message.document
            if not doc.file_name.lower().endswith(".json"):
                await update.message.reply_text("❌ Please send the <code>m365_session.json</code> file.", parse_mode="HTML")
                return

            file = await context.bot.get_file(doc.file_id)
            data = bytes(await file.download_as_bytearray())

            import json as _json
            state_json = data.decode("utf-8")
            _json.loads(state_json)  # validate before storing

            # Persistence check: warn if this looks like a short-lived
            # (session-only) login that Microsoft will drop within ~24h.
            try:
                from services.m365_family import analyze_session_persistence
                _persist = analyze_session_persistence(state_json)
            except Exception:
                _persist = None

            from database.db import add_m365_session, rename_m365_session, list_m365_sessions
            caption = (update.message.caption or "").strip()
            new_sess_id = add_m365_session("", state_json, _sess_warranty)
            _w_prefix = {"6m": "6M ", "1y": "1Y "}.get(_sess_warranty, "")
            sess_label = caption[:60] if caption else _w_prefix + "ID #" + str(new_sess_id)
            rename_m365_session(new_sess_id, sess_label)
            total_sessions = len(list_m365_sessions())

            user_state[uid] = "admin_m365_sess_label:" + str(new_sess_id)

            await update.message.reply_text(
                "✅ <b>Microsoft session saved (encrypted).</b>\n\n"
                f"🆔 Stored as: <b>{html.escape(sess_label, quote=False)}</b>\n"
                + ({"6m": "🏷 Warranty group: <b>6-Month</b> — used only for 6-Month invite products.\n", "1y": "🏷 Warranty group: <b>1-Year</b> — used only for 1-Year invite products.\n"}.get(_sess_warranty, ""))
                + f"📂 Total IDs stored: <b>{total_sessions}</b>\n\n"
                "✏️ Now send the <b>Microsoft account email/name</b> for this ID so rotation alerts are easy to read — or send <code>-</code> to keep the automatic name.\n\n"
                "🧪 Then use Admin Panel → 📩 M365 Auto-Invite → Test Invite to verify it works.\n\n"
                + ((_persist["warning_html"] + "\n\n") if (_persist and _persist.get("warning_html")) else "")
                + "💡 For safety, delete <code>m365_session.json</code> from your PC after this.",
                reply_markup=main_menu(uid),
                parse_mode="HTML"
            )
            return

        except Exception as exc:
            await update.message.reply_text(
                f"❌ <b>Session save failed.</b>\n\n<code>{exc}</code>\n\nMake sure you sent the unmodified <code>m365_session.json</code> file.",
                parse_mode="HTML"
            )
            return

    if state == "admin_upload_canva_session" and is_admin(uid):
        try:
            doc = update.message.document
            if not doc.file_name.lower().endswith(".json"):
                await update.message.reply_text("❌ Please send the <code>canva_session.json</code> file.", parse_mode="HTML")
                return

            file = await context.bot.get_file(doc.file_id)
            data = bytes(await file.download_as_bytearray())

            from services.canva_invite import save_session
            save_session(data.decode("utf-8"))
            user_state.pop(uid, None)

            await update.message.reply_text(
                "✅ <b>Canva session saved (encrypted).</b>\n\n"
                "🧪 Now use Admin Panel → 🎨 Canva Auto-Invite → Test Invite to verify it works.\n\n"
                "💡 For safety, delete <code>canva_session.json</code> from your PC after this.",
                reply_markup=main_menu(uid),
                parse_mode="HTML"
            )
            return

        except Exception as exc:
            await update.message.reply_text(
                f"❌ <b>Session save failed.</b>\n\n<code>{exc}</code>\n\nMake sure you sent the unmodified <code>canva_session.json</code> file.",
                parse_mode="HTML"
            )
            return

    if state == "admin_restore_backup" and is_admin(uid):
        try:
            doc = update.message.document
            if not doc.file_name.lower().endswith(".zip"):
                await update.message.reply_text("❌ Please send a .zip backup file.", parse_mode="HTML")
                return

            file = await context.bot.get_file(doc.file_id)
            restore_dir = Path("restore_uploads")
            restore_dir.mkdir(exist_ok=True)
            file_path = restore_dir / doc.file_name
            await file.download_to_drive(str(file_path))

            ok, msg = restore_backup_zip(str(file_path))
            user_state.pop(uid, None)

            await update.message.reply_text(
                ("✅ " if ok else "❌ ") + f"<b>{msg}</b>",
                reply_markup=main_menu(uid),
                parse_mode="HTML"
            )
            return

        except Exception as exc:
            await update.message.reply_text(
                f"❌ <b>Restore failed.</b>\n\n<code>{exc}</code>",
                parse_mode="HTML"
            )
            return

    await update.message.reply_text("ℹ️ Document received, but no document action is active.", parse_mode="HTML")
