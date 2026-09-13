from telegram import InlineKeyboardMarkup
from utils.button_styles import AutoStyledButton as InlineKeyboardButton
import html
import time
import io
import csv
import os
import shutil
from datetime import datetime, timedelta

from database.db import create_scheduled_broadcast, get_scheduled_broadcasts, render_message_template, cur, conn, is_admin, is_owner, add_admin, remove_admin, get_admins, add_balance, add_stock_items, get_available_stock_count, get_sold_stock_count, get_total_stock_count, restock_product, block_user, unblock_user, log_admin_action, get_admin_logs, update_product_details, get_open_tickets, get_ticket, reply_ticket, close_ticket, get_pending_withdrawals, update_withdrawal_status, set_setting, get_setting, set_product_featured, remove_product_image, get_product_price_tiers, set_product_price_tier, delete_product_price_tier, clear_product_price_tiers, validate_coupon, list_message_templates, get_message_template, set_message_template, smart_dashboard_stats, clear_admin_logs, create_backup_zip, restore_backup_zip, create_bundle, get_all_bundles, get_bundle_items, delete_bundle, get_m365_product_ids, toggle_m365_product, list_m365_invites, get_m365_invite, set_m365_invite_status, get_canva_product_ids, toggle_canva_product, list_canva_invites, get_canva_invite, set_canva_invite_status, get_m365_product_warranty, set_m365_product_warranty, set_m365_session_warranty
from utils.crypto import decrypt_text
from database.db import get_display_stock, tiers_recently_edited
from database.db import get_pending_replacements, get_replacement, set_replacement_status, set_warranty_ban, is_warranty_banned, count_recent_replacements, reserve_stock_item, create_gift_code, list_gift_codes, rename_m365_session, list_m365_sessions, get_current_m365_session, reset_m365_session, delete_m365_session
from keyboards.menus import admin_menu, back_home, admin_products_menu, admin_orders_users_menu, admin_marketing_menu, admin_system_menu, admin_support_menu
from config import DB_PATH, ADMIN_ID
from utils.ui import edit_or_send
from utils.premium_emoji import premium_html, strip_premium, first_premium_id, sanitize_premium_name, button_premium, premium_safe
from database.db import search_orders, count_search_orders, get_order_admin_detail, get_order_payment_method, get_order_status_label, find_user_by_id, find_users_by_username, get_user_overview, get_user_deposits_page, payment_method_label, get_categories, get_category, get_category_by_name, create_category, rename_category, delete_category, assign_product_category
import math


def _admin_name(value):
    """HTML-safe product name that KEEPS premium/custom emoji tags.

    Previously this stripped <tg-emoji> tags, which is why premium emojis
    disappeared from broadcasts (price drop, new offer, refunds, etc.).
    premium_safe() escapes everything else so HTML parsing never breaks."""
    return premium_safe(str(value))
from keyboards.menus import MAIN_MENU_BUTTONS
from utils.states import user_state
from utils.timezone import now, now_text, today_prefix, month_prefix, display_time

admin_temp_data = {}

def smart_dashboard_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Products", callback_data="admin_section_products"), InlineKeyboardButton("🛒 Orders", callback_data="admin_orders")],
        [InlineKeyboardButton("🎫 Tickets", callback_data="admin_ticket_manager"), InlineKeyboardButton("⚠️ Low Stock", callback_data="admin_low_stock")],
        [InlineKeyboardButton("📈 Sales Analytics", callback_data="admin_sales_analytics"), InlineKeyboardButton("🤖 Auto Marketing", callback_data="admin_auto_marketing")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin_smart_dashboard")],
    ])


def ticket_manager_keyboard(ticket_id=None, user_id=None):
    if ticket_id and user_id:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Reply", callback_data=f"admin_ticket_reply_{ticket_id}")],
            [InlineKeyboardButton("✅ Close", callback_data=f"admin_ticket_close_{ticket_id}")],
            [InlineKeyboardButton("🚫 Ban User", callback_data=f"admin_ticket_ban_{user_id}")],
            [InlineKeyboardButton("⬅️ Tickets", callback_data="admin_ticket_manager")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin_ticket_manager")],
        [InlineKeyboardButton("⬅️ Support Menu", callback_data="admin_section_support")],
    ])


def auto_marketing_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 Welcome Coupon", callback_data="admin_set_welcome_coupon")],
        [InlineKeyboardButton("😴 Inactive Coupon", callback_data="admin_set_inactive_coupon")],
        [InlineKeyboardButton("📅 Inactive Days", callback_data="admin_set_inactive_days")],
        [InlineKeyboardButton("⬅️ Marketing", callback_data="admin_section_marketing")],
    ])


def auto_marketing_text():
    welcome = get_setting("auto_welcome_coupon", "")
    inactive = get_setting("inactive_coupon", "")
    days = get_setting("inactive_days", "7")
    return f"""🤖 <b>Auto Marketing System</b>

🎁 <b>New User Welcome Coupon:</b> <code>{welcome or 'OFF'}</code>
😴 <b>Inactive User Coupon:</b> <code>{inactive or 'OFF'}</code>
📅 <b>Inactive After:</b> <code>{days} days</code>

Notes:
• Welcome coupon is sent when a new user starts the bot.
• Inactive coupon is checked by marketing worker.
• Set empty value to disable a coupon trigger."""


def scheduled_broadcast_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Schedule Broadcast", callback_data="admin_schedule_broadcast")],
        [InlineKeyboardButton("📋 View Scheduled", callback_data="admin_view_scheduled_broadcasts")],
        [InlineKeyboardButton("⬅️ Marketing", callback_data="admin_section_marketing")],
    ])


def _revenue_reset_at():
    """Revenue stats only count orders created after this timestamp.

    Stored in UTC because orders.created uses CURRENT_TIMESTAMP (UTC).
    Empty setting = all-time stats (never reset).
    """
    return get_setting("revenue_reset_at", "") or "1970-01-01 00:00:00"


def _utc_now_text():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def sales_analytics_text():
    reset_at = _revenue_reset_at()
    cur.execute("SELECT COALESCE(SUM(price),0), COUNT(*) FROM orders WHERE created >= ?", (reset_at,))
    total_revenue, total_orders = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM users")
    users_count = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(amount),0), COUNT(*) FROM deposits WHERE status='paid' AND created >= ?", (reset_at,))
    dep_total, dep_count = cur.fetchone()
    cur.execute("""
        SELECT product_name, COUNT(*) c, COALESCE(SUM(price),0) r
        FROM orders
        WHERE created >= ?
        GROUP BY product_name
        ORDER BY r DESC
        LIMIT 5
    """, (reset_at,))
    products = cur.fetchall()

    product_text = ""
    for i, (name, count, revenue) in enumerate(products, 1):
        product_text += f"\n{i}. {name[:35]} — <code>${float(revenue):.2f}</code> / <code>{count}</code> orders"

    if not product_text:
        product_text = "\nNo sales yet."

    avg_order = float(total_revenue or 0) / int(total_orders or 1)

    return f"""📈 <b>Sales Analytics Dashboard</b>

💰 <b>Total Revenue:</b> <code>${float(total_revenue or 0):.2f}</code>
🛒 <b>Total Orders:</b> <code>{total_orders}</code>
👥 <b>Total Users:</b> <code>{users_count}</code>
💳 <b>Paid Deposits:</b> <code>{dep_count}</code> | <code>${float(dep_total or 0):.2f}</code>
📊 <b>Average Order:</b> <code>${avg_order:.2f}</code>

🏆 <b>Top Revenue Products</b>{product_text}"""


def message_templates_keyboard():
    rows = []
    for key, title, updated in list_message_templates():
        if key == "welcome":
            continue  # welcome message is edited via 🎨 Customize Bot
        rows.append([InlineKeyboardButton(f"✏️ {title}", callback_data=f"admin_edit_template_{key}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_menu")])
    return InlineKeyboardMarkup(rows)


def template_help_text(key: str):
    placeholders = {
        "welcome": "{store}",
        "delivery_success": "{order_id}, {product}, {code}, {paid}, {balance}, {user}",
        "coupon_broadcast": "{code}, {discount}, {validity}, {limit}",
        "flash_sale_broadcast": "{product}, {old_price}, {new_price}, {discount}, {validity}, {stock}",
        "restock_alert": "{product}, {stock}",
        "deposit_success": "{invoice_id}, {paid}, {amount}, {balance}, {tx}",
        "deposit_prompt": "{balance}, {min}, {max}",
        "profile_message": "{user_id}, {name}, {username}, {balance}, {orders}, {spent}, {deposits}, {deposited}, {referral_earnings}, {referral_pending}, {coupon}, {notifications}",
        "wallet_message": "{user_id}, {name}, {username}, {balance}, {deposited}, {deposits}, {orders}, {spent}, {referral_earnings}, {referral_pending}, {loyalty_points}, {vip_rank}",
        "support_prompt": "{name}",
        "new_product_broadcast": "{product}, {added}, {available}, {price}",
        "stock_added_broadcast": "{product}, {added}, {available}, {price}",
    }
    return placeholders.get(key, "Type your message normally.")


def template_example_text(key: str):
    examples = {
        "delivery_success": "✅ Payment Successful!\n\n📦 {product}\n\n🔑 Code:\n{code}\n\n💰 Paid: {paid}\n💼 Balance: {balance}\n\nThank you ❤️",
        "welcome": "👋 Welcome to {store}\n\nChoose an option below 👇",
        "coupon_broadcast": "🎟 New Coupon!\n\nCode: {code}\nDiscount: {discount}\nValid: {validity}",
        "flash_sale_broadcast": "🔥 Flash Sale!\n\n📦 {product}\nWas: {old_price}\nNow: {new_price}\n⏳ {validity}",
        "restock_alert": "🔔 Back in stock!\n\n📦 {product}\nAvailable: {stock}",
        "deposit_success": "✅ DEPOSIT CONFIRMED\n\n🧾 Invoice: #{invoice_id}\n💵 Paid: {paid} USDT\n💰 Added: {amount} USDT\n💼 Balance: {balance} USDT",
        "new_product_broadcast": "🆕 New Product!\n\n📦 {product}\n✅ Available: {available}\n💰 Price: {price}",
        "stock_added_broadcast": "🆕 Stock Added!\n\n📦 {product}\n➕ Added: {added}\n✅ Available: {available}",
        "deposit_prompt": "💰 Add Funds\n\nCurrent balance: {balance} USDT\n\nSend the amount you want to deposit.",
        "profile_message": "👤 My Profile\n\n🆔 {user_id}\n💼 Balance: {balance} USDT\n🛒 Orders: {orders}",
        "wallet_message": "💼 My Wallet\n\n💰 Balance: {balance} USDT\n📥 Deposited: {deposited} USDT",
        "support_prompt": "🎫 Create Support Ticket\n\nSend your issue/message now.",
    }
    return examples.get(key, "Type the exact message you want.")


def admin_sales_settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎉 First Purchase Discount", callback_data="admin_set_first_discount")],
        [InlineKeyboardButton("💎 Loyalty Percent", callback_data="admin_set_loyalty_percent")],
        [InlineKeyboardButton("🎁 Buy X Get Y", callback_data="admin_set_buyxgety")],
        [InlineKeyboardButton("⬅️ Marketing Menu", callback_data="admin_section_marketing")],
    ])


def sales_settings_text():
    first = get_setting("first_purchase_discount_percent", "0")
    loyalty = get_setting("loyalty_percent", "2")
    buyx = get_setting("buy_x_qty", "0")
    free = get_setting("buy_x_free", "0")
    return f"""💎 <b>Sales Settings</b>

🎉 First Purchase Discount: <code>{first}%</code>
💎 Loyalty Cashback Points: <code>{loyalty}%</code>
🎁 Buy X Get Y: <code>Buy {buyx}, Get {free} Free</code>

Choose what to edit:"""


def user_management_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban_user")],
        [InlineKeyboardButton("✅ Unban User", callback_data="admin_unban_user")],
        [InlineKeyboardButton("⬅️ Orders & Users", callback_data="admin_section_orders_users")],
    ])



BROADCAST_SEND_DELAY_SECONDS = 0.05


def _queue_broadcast(context, recipients, msg, kb=None, tag="BROADCAST", detail=""):
    """Send a bulk broadcast in the BACKGROUND, paced under Telegram's limits.

    python-telegram-bot processes updates sequentially, so awaiting hundreds
    of send_message() calls directly inside a handler freezes the ENTIRE bot
    until the broadcast drains -- /start and /admin never get answered. This
    schedules the sends as a background task instead and paces them, so the
    bot stays responsive.

    Returns the number of recipients QUEUED (delivery happens afterwards).
    """
    import asyncio
    from html import escape as html_escape
    from telegram.error import RetryAfter, Forbidden, TelegramError

    def _chat_id(row):
        """Extract a numeric chat_id from a DB row.

        Rows are NOT always tuples: sqlite3 yields sqlite3.Row and the
        Postgres compatibility layer yields pgcompat._Row. Neither is a
        list/tuple subclass, so never branch on isinstance(row, tuple) --
        that silently passes the whole row object as chat_id and makes every
        single send fail.
        """
        if isinstance(row, bool):
            raise TypeError("bool is not a chat_id")
        if isinstance(row, int):
            return row
        if isinstance(row, str):
            return int(row.strip())
        try:
            return int(row[0])
        except (TypeError, KeyError, IndexError):
            return int(next(iter(row)))

    # Materialise the rows now: the shared DB cursor must not be reused
    # across await points inside the background task.
    targets = []
    skipped = 0
    for _row in recipients:
        try:
            targets.append(_chat_id(_row))
        except Exception:
            skipped += 1
    total = len(targets)

    if skipped:
        try:
            log_admin_action(0, tag, "%s WARNING unreadable_rows=%s" % (detail, skipped))
        except Exception:
            pass

    async def _send_one(target_id):
        await context.bot.send_message(
            chat_id=target_id,
            text=msg,
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def _run():
        sent = 0
        failed = 0
        first_error = None

        def _note(exc):
            nonlocal first_error
            if first_error is None:
                first_error = "%s: %s" % (type(exc).__name__, exc)

        for target_id in targets:
            try:
                await _send_one(target_id)
                sent += 1
            except RetryAfter as exc:
                # Telegram flood control: wait the requested time, retry once.
                try:
                    await asyncio.sleep(float(getattr(exc, "retry_after", 5)) + 1)
                    await _send_one(target_id)
                    sent += 1
                except Exception as exc2:
                    failed += 1
                    _note(exc2)
            except (Forbidden, TelegramError) as exc:
                failed += 1  # blocked the bot / deactivated / bad chat
                _note(exc)
            except Exception as exc:
                failed += 1
                _note(exc)

            # Yield to the event loop and stay under ~30 msg/sec.
            await asyncio.sleep(BROADCAST_SEND_DELAY_SECONDS)

        try:
            log_admin_action(
                0,
                tag,
                "%s sent=%s failed=%s%s"
                % (detail, sent, failed, " err=%s" % first_error if first_error else ""),
            )
        except Exception:
            pass

        summary = (
            "\u2705 <b>Broadcast finished</b>\n\n"
            "\U0001f4e8 <b>Sent:</b> <code>%s</code>\n"
            "\u26a0\ufe0f <b>Failed:</b> <code>%s</code>\n"
            "\U0001f3af <b>Total:</b> <code>%s</code>" % (sent, failed, total)
        )
        # A total wipeout is a code fault, not users blocking the bot. Show it.
        if sent == 0 and failed and first_error:
            summary += "\n\n\u274c <b>First error:</b>\n<code>%s</code>" % (
                html_escape(first_error[:300])
            )

        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=summary,
                parse_mode="HTML",
            )
        except Exception:
            pass

    try:
        context.application.create_task(_run())
    except Exception:
        try:
            asyncio.get_event_loop().create_task(_run())
        except Exception:
            return 0

    return total


async def broadcast_coupon_created(context, code: str, dtype: str, value: float, max_uses: int, expires_at: str):
    """Broadcast newly created coupon to all users."""
    try:
        cur.execute("SELECT user_id FROM users WHERE COALESCE(blocked,0)=0 AND COALESCE(notifications,1)=1")
        users = cur.fetchall()

        if dtype == "percent":
            discount_text = f"{value:g}% OFF"
        else:
            discount_text = f"${value:.2f} OFF"

        validity_text = "Limited time"
        try:
            end_dt = datetime.strptime(str(expires_at), "%Y-%m-%d %H:%M:%S")
            remaining = end_dt - datetime.strptime(now().strftime("%Y-%m-%d %H:%M:%S"), "%Y-%m-%d %H:%M:%S")
            hours = max(1, int(remaining.total_seconds() // 3600))
            validity_text = f"Valid for {hours}h"
        except Exception:
            if expires_at:
                validity_text = str(expires_at)

        uses_text = "Unlimited uses" if not max_uses else f"Only {max_uses} uses"

        msg = render_message_template(
            "coupon_broadcast",
            "🎟 <b>NEW COUPON AVAILABLE!</b>\n\n💝 <b>Discount:</b> <code>{discount}</code>\n🎫 <b>Coupon Code:</b> <code>{code}</code>\n⏳ <b>Validity:</b> <code>{validity}</code>\n🔥 <b>Limit:</b> <code>{limit}</code>\n\nApply this coupon before checkout and save on your next purchase 👇",
            discount=discount_text,
            code=code,
            validity=validity_text,
            limit=uses_text,
        )

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛍 Shop Now", callback_data="shop")],
            [InlineKeyboardButton("🎟 Apply Coupon", callback_data="apply_coupon")]
        ])

        queued = _queue_broadcast(context, users, msg, kb, "COUPON_BROADCAST", f"code={code}")
        return queued, 0

    except Exception:
        return 0, 0



def admin_coupons_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Create Coupon", callback_data="admin_coupon_create")],
        [InlineKeyboardButton("🤝 Reseller Coupon", callback_data="admin_reseller_coupon_create")],
        [InlineKeyboardButton("📋 List Coupons", callback_data="admin_coupon_list")],
        [InlineKeyboardButton("🗑 Delete Coupon", callback_data="admin_coupon_delete")],
        [InlineKeyboardButton("⬅️ Marketing Menu", callback_data="admin_section_marketing")],
    ])


def product_analytics_text(product_id: int):
    cur.execute("SELECT name, price, stock FROM products WHERE id=?", (product_id,))
    p = cur.fetchone()
    if not p:
        return None
    name, price, stock = p
    live_stock = get_available_stock_count(product_id)
    cur.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE product_name LIKE ? AND created >= ?", (f"%{name}%", _revenue_reset_at()))
    orders, revenue = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=1", (product_id,))
    sold_stock = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=0", (product_id,))
    available = cur.fetchone()[0]

    return f"""📈 <b>Product Analytics</b>

📦 <b>{_admin_name(name)}</b>
🆔 Product ID: <code>{product_id}</code>
💰 Price: <code>${float(price):.2f}</code>

🛒 Orders: <code>{orders}</code>
💵 Revenue: <code>${float(revenue or 0):.2f}</code>
📤 Delivered/Sold Stock: <code>{sold_stock}</code>
Available Stock: <code>{available or live_stock or stock}</code>"""


def top_sellers_text():
    cur.execute("""
        SELECT product_name, COUNT(*) c, COALESCE(SUM(price),0) r
        FROM orders
        WHERE created >= ?
        GROUP BY product_name
        ORDER BY c DESC, r DESC
        LIMIT 10
    """, (_revenue_reset_at(),))
    rows = cur.fetchall()

    text = "🏆 <b>Top Selling Products</b>\n\n"
    if not rows:
        return text + "No sales yet."

    for i, (name, count, revenue) in enumerate(rows, 1):
        text += f"{i}. <b>{name[:45]}</b>\n🛒 <code>{count}</code> orders | 💰 <code>${float(revenue):.2f}</code>\n\n"
    return text


def cleanup_report_text():
    cur.execute("DELETE FROM user_coupons WHERE code NOT IN (SELECT code FROM coupons)")
    orphan_coupons = cur.rowcount
    cur.execute("UPDATE products SET flash_price=NULL, flash_ends_at=NULL WHERE flash_ends_at IS NOT NULL AND flash_ends_at < ?", (now_text(),))
    expired_sales = cur.rowcount
    conn.commit()

    return f"""🧹 <b>Cleanup Complete</b>

🎟 Removed orphan user coupons: <code>{orphan_coupons}</code>
🔥 Cleared expired flash sales: <code>{expired_sales}</code>

Database cleaned safely."""

def _now_text():
    return now_text()


def _parse_hours(hours_text):
    hours = float(hours_text)
    if hours <= 0:
        hours = 24
    return hours


def flash_sale_text(product_id: int):
    cur.execute("SELECT id, name, price, flash_price, flash_ends_at FROM products WHERE id=?", (product_id,))
    p = cur.fetchone()
    if not p:
        return None
    pid, name, price, flash_price, flash_ends_at = p
    status = "OFF"
    if flash_price and flash_ends_at:
        try:
            # Sortable-text comparison; comparing strptime() with the timezone-aware
            # now() raised TypeError, so status always showed OFF.
            if str(flash_ends_at).replace("T", " ")[:19] > now_text():
                status = "🔥 ACTIVE"
        except Exception:
            pass
    return f"""🔥 <b>Flash Sale</b>

📦 <b>Product:</b> {name}
🆔 <b>ID:</b> <code>{pid}</code>
💰 <b>Normal Price:</b> <code>${float(price):.2f}</code>
🔥 <b>Flash Price:</b> <code>{('$' + format(float(flash_price), '.2f')) if flash_price else 'Not set'}</code>
⏳ <b>Ends:</b> <code>{flash_ends_at or 'Not set'}</code>
🚦 <b>Status:</b> {status}

Choose an action:"""


def flash_sale_keyboard(product_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Set Flash Sale", callback_data=f"admin_flash_set_{product_id}")],
        [InlineKeyboardButton("🛑 Stop Flash Sale", callback_data=f"admin_flash_stop_{product_id}")],
        [InlineKeyboardButton("⬅️ Choose Product", callback_data="admin_flash_sales")],
        [InlineKeyboardButton("🏠 Products Menu", callback_data="admin_section_products")],
    ])


async def broadcast_flash_sale(context, product_id: int, name: str, old_price: float, new_price: float, ends_at: str):
    try:
        cur.execute("SELECT user_id FROM users WHERE COALESCE(blocked,0)=0 AND COALESCE(notifications,1)=1")
        users = cur.fetchall()

        discount = 0
        if old_price > 0:
            discount = round((old_price - new_price) / old_price * 100)

        try:
            ends_dt = datetime.strptime(ends_at, "%Y-%m-%d %H:%M:%S")
            remaining = ends_dt - datetime.strptime(now().strftime("%Y-%m-%d %H:%M:%S"), "%Y-%m-%d %H:%M:%S")
            hours_left = max(1, int(remaining.total_seconds() // 3600))
            ends_text = f"ends in {hours_left}h"
        except Exception:
            ends_text = "limited time"

        msg = render_message_template(
            "flash_sale_broadcast",
            "🧨 <b>FLASH SALE — LIMITED TIME!</b>\n\n📦 <b>{product_name}</b>\n\n💵 <b>Was:</b> <s>${old_price}</s>\n⚡ <b>Now:</b> <code>${new_price} USDT</code> <b>({discount_percent}% OFF)</b>\n\n🏃 <b>Hurry — {validity}</b>\nGrab it before the timer runs out 👇",
            product_name=_admin_name(name),
            old_price=f"${old_price:.2f}",
            new_price=f"${new_price:.2f}",
            discount_text=f"{discount}% OFF" if discount else "SALE",
            discount_percent=f"-{discount}" if discount else "SALE",
            validity=ends_text,
            stock=get_display_stock(product_id),
        )

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🛒 Buy {button_premium(name)[0][:28]}", callback_data=f"product_{product_id}", icon_custom_emoji_id=button_premium(name)[1])]
        ])

        queued = _queue_broadcast(context, users, msg, kb, "FLASH_SALE_BROADCAST", f"product={product_id}")
        return queued, 0

    except Exception:
        return 0, 0


def admin_stats_text():
    today = today_prefix()
    month = month_prefix()
    reset_at = _revenue_reset_at()
    cur.execute("SELECT COALESCE(SUM(price),0), COUNT(*) FROM orders WHERE created >= ?", (reset_at,))
    total_revenue, total_orders = cur.fetchone()
    cur.execute("SELECT COALESCE(SUM(price),0), COUNT(*) FROM orders WHERE created LIKE ? AND created >= ?", (today + "%", reset_at))
    today_revenue, today_orders = cur.fetchone()
    cur.execute("SELECT COALESCE(SUM(price),0), COUNT(*) FROM orders WHERE created LIKE ? AND created >= ?", (month + "%", reset_at))
    month_revenue, month_orders = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM products WHERE active=1")
    active_products = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(stock),0) FROM products WHERE active=1")
    visible_stock = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM deposits WHERE status='paid' AND created >= ?", (reset_at,))
    paid_deposits, deposit_value = cur.fetchone()
    cur.execute("SELECT product_name, COUNT(*) c, COALESCE(SUM(price),0) r FROM orders WHERE created >= ? GROUP BY product_name ORDER BY c DESC LIMIT 5", (reset_at,))
    top = cur.fetchall()

    top_text = ""
    if top:
        for i, (product, count, revenue) in enumerate(top, 1):
            top_text += f"\n{i}. {product[:35]} — <code>{count}</code> sales | <code>${float(revenue):.2f}</code>"
    else:
        top_text = "\nNo sales yet."

    return f"""📊 <b>Statistics Dashboard</b>

<b>Today</b>
🛒 Orders: <code>{today_orders}</code>
💰 Revenue: <code>${float(today_revenue):.2f}</code>

<b>This Month</b>
🛒 Orders: <code>{month_orders}</code>
💰 Revenue: <code>${float(month_revenue):.2f}</code>

<b>All Time</b>
🛒 Orders: <code>{total_orders}</code>
💰 Revenue: <code>${float(total_revenue):.2f}</code>
👥 Users: <code>{total_users}</code>
📦 Active Products: <code>{active_products}</code>
📊 Visible Stock: <code>{visible_stock}</code>
💳 Paid Deposits: <code>{paid_deposits}</code> | <code>${float(deposit_value):.2f}</code>

🏆 <b>Top Products</b>{top_text}"""


def admin_stats_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin_stats_dashboard")],
        [InlineKeyboardButton("⚠️ Low Stock", callback_data="admin_low_stock")],
        [InlineKeyboardButton("♻️ Reset Revenue", callback_data="admin_revenue_reset")],
        [InlineKeyboardButton("⬅️ System Menu", callback_data="admin_section_system")],
    ])


def order_search_results_keyboard(rows):
    kb = []
    for oid, user_id, product_name, price, created in rows:
        kb.append([InlineKeyboardButton(f"#{oid} • ${float(price):.2f} • {str(product_name)[:26]}", callback_data=f"admin_order_detail_{oid}")])
    kb.append([InlineKeyboardButton("🔎 Search Again", callback_data="admin_search_order_menu")])
    kb.append([InlineKeyboardButton("⬅️ Orders", callback_data="admin_orders")])
    return InlineKeyboardMarkup(kb)


def admin_orders_list_keyboard(limit: int = 20):
    cur.execute("SELECT id, user_id, product_name, price, created FROM orders ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    kb = []
    for oid, user_id, product_name, price, created in rows:
        kb.append([InlineKeyboardButton(f"#{oid} • ${price:.2f} • {str(product_name)[:28]}", callback_data=f"admin_order_detail_{oid}")])
    kb.append([InlineKeyboardButton("🔎 Search Orders", callback_data="admin_search_orders")])
    kb.append([InlineKeyboardButton("⬅️ Orders & Users", callback_data="admin_section_orders_users")])
    return InlineKeyboardMarkup(kb)

def admin_order_detail_keyboard(order_id: int, user_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Resend Delivery", callback_data=f"admin_order_resend_{order_id}")],
        [InlineKeyboardButton("💸 Refund Order", callback_data=f"admin_order_refund_{order_id}")],
        [InlineKeyboardButton("👤 View User", callback_data=f"admin_order_user_{user_id}")],
        [InlineKeyboardButton("🗑 Delete Order Log", callback_data=f"admin_order_delete_{order_id}")],
        [InlineKeyboardButton("⬅️ Back To Orders", callback_data="admin_orders")],
    ])

def get_order_detail_text(order_id: int):
    order = get_order_admin_detail(order_id)
    if not order:
        return None, None, None
    oid, user_id, product_name, price, delivery, created, method_stored, product_id, username, first_name = order
    delivery = decrypt_text(delivery)
    cur.execute("SELECT balance, blocked FROM users WHERE user_id=?", (user_id,))
    u = cur.fetchone()
    if u:
        balance, blocked = u
        uname = f"@{username}" if username and username != "No username" else "No username"
        status = "🚫 Blocked" if blocked else "✅ Active"
    else:
        first_name = first_name or "Unknown"
        uname, balance, status = "No username", 0, "Unknown"
    qty = 1
    if " x" in str(product_name):
        try: qty = int(str(product_name).split(" x")[-1])
        except Exception: qty = 1
    pay_method = get_order_payment_method(oid, user_id, product_id, method_stored)
    order_status = get_order_status_label(oid)
    # Telegram messages cap at 4096 chars: never let a huge bulk delivery
    # break the detail view. Admins can still resend/download the full text.
    delivery_block = html.escape(str(delivery or ""), quote=False)
    if len(delivery_block) > 3000:
        delivery_block = delivery_block[:3000] + "\n… (truncated here -- use 🔄 Resend Delivery for the full text)"
    text = f"""🧾 <b>Order Detail</b>

🆔 <b>Order ID:</b> <code>#{oid}</code>
📦 <b>Product:</b> {_admin_name(product_name)}
🔢 <b>Quantity:</b> <code>{qty}</code>
💰 <b>Amount:</b> <code>${price:.2f}</code>
💳 <b>Payment Method:</b> {pay_method}
🚦 <b>Status:</b> {order_status}
🕒 <b>Date:</b> <code>{display_time(created) or created}</code>

━━━━━━━━━━━━━━
👤 <b>User</b>

🆔 <code>{user_id}</code>
👤 <b>Name:</b> {html.escape(str(first_name or 'User'), quote=False)}
🔗 <b>Username:</b> {uname}
💼 <b>Balance:</b> <code>${float(balance or 0):.2f}</code>
🚦 <b>Account:</b> {status}

━━━━━�����━━━━━━━━
🔑 <b>Delivered Code/Account</b>

<code>{delivery_block}</code>"""
    return text, user_id, delivery

# ===================== Admin search: orders & users =====================

ORDER_SEARCH_PAGE_SIZE = 5
ADMIN_USER_PAGE_SIZE = 5


class _MsgQuery:
    """Adapt a message context to the query-like object edit_or_send expects
    (search results first arrive via a typed message, not a button tap)."""
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


def admin_order_search_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧾 By Order ID", callback_data="admin_osearch_id"),
         InlineKeyboardButton("👤 By User ID", callback_data="admin_osearch_uid")],
        [InlineKeyboardButton("🔖 By Username", callback_data="admin_osearch_user"),
         InlineKeyboardButton("📅 By Date / Range", callback_data="admin_osearch_date")],
        [InlineKeyboardButton("⬅️ Orders & Users", callback_data="admin_section_orders_users")],
    ])


def _osearch_prompt_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_search_orders")]])


def _osearch_kwargs(mode, value):
    """Search context (mode, value) -> (search_orders kwargs, human label)."""
    if mode == "uid":
        return {"user_id": int(value)}, f"user ID <code>{value}</code>"
    if mode == "user":
        return {"username": value}, f"username <code>@{html.escape(str(value), quote=False)}</code>"
    if mode == "date":
        date_from, date_to = value
        label = f"📅 <code>{date_from}</code>" + (f" → <code>{date_to}</code>" if date_to != date_from else "")
        return {"date_from": date_from, "date_to": date_to}, label
    return {}, "all orders"


async def _show_order_search_results(q, uid, ctx, page):
    """Render one page of order search results. Only ORDER_SEARCH_PAGE_SIZE
    rows are ever queried -- the full orders table is never loaded."""
    mode, value = ctx
    kwargs, label = _osearch_kwargs(mode, value)
    total = count_search_orders(**kwargs)
    pages = max(1, math.ceil(total / ORDER_SEARCH_PAGE_SIZE))
    page = max(0, min(int(page), pages - 1))
    rows = search_orders(limit=ORDER_SEARCH_PAGE_SIZE, offset=page * ORDER_SEARCH_PAGE_SIZE, **kwargs)

    text = (
        f"🔎 <b>Order Search</b> — {label}\n\n"
        f"Found: <code>{total}</code> order(s) | Page <code>{page + 1}/{pages}</code>\n\n"
    )
    kb = []
    if not rows:
        text += "No orders found."
    for oid, user_id, product_name, price, created, method_stored, product_id, username, first_name in rows:
        uname = f"@{username}" if username and username != "No username" else "No username"
        text += (
            f"🧾 <b>#{oid}</b> — {_admin_name(product_name)}\n"
            f"👤 <code>{user_id}</code> • {uname} • 💰 <code>${float(price):.2f}</code>\n"
            f"🕒 <code>{display_time(created) or created}</code>\n\n"
        )
        kb.append([InlineKeyboardButton(f"🧾 Order #{oid} details", callback_data=f"admin_order_detail_{oid}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"admin_osearch_page_{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("▶️ Next", callback_data=f"admin_osearch_page_{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔎 New Search", callback_data="admin_search_orders")])
    kb.append([InlineKeyboardButton("⬅️ Orders & Users", callback_data="admin_section_orders_users")])

    await edit_or_send(q, text, InlineKeyboardMarkup(kb))


def admin_user_card_keyboard(target):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Orders", callback_data=f"admin_usr_orders_{target}_0"),
         InlineKeyboardButton("💰 Deposits", callback_data=f"admin_usr_deps_{target}_0")],
        [InlineKeyboardButton("📊 Statistics", callback_data=f"admin_usr_stats_{target}")],
        [InlineKeyboardButton("🔎 New Search", callback_data="admin_search_user"),
         InlineKeyboardButton("⬅️ Orders & Users", callback_data="admin_section_orders_users")],
    ])


def _admin_user_card_text(overview):
    # list() first: the row can be a pgcompat _Row on PostgreSQL, which
    # supports int/str indexing but NOT slicing -- list() works everywhere.
    user_id, first_name, username, balance, blocked, joined = list(overview["user"])[:6]
    uname = f"@{username}" if username and username != "No username" else "No username"
    status = "🚫 Blocked" if blocked else "✅ Active"
    return f"""👤 <b>User Information</b>

🆔 <b>User ID:</b> <code>{user_id}</code>
🔗 <b>Username:</b> {uname}
👤 <b>Name:</b> {html.escape(str(first_name or "User"), quote=False)}
💼 <b>Balance:</b> <code>{float(balance or 0):.2f} USDT</code>
🚦 <b>Status:</b> {status}
🕒 <b>Joined:</b> <code>{display_time(joined) or joined}</code>

🛒 <b>Total Orders:</b> <code>{overview["orders_count"]}</code>
💸 <b>Total Spent:</b> <code>${overview["total_spent"]:.2f}</code>
📥 <b>Total Deposited:</b> <code>{overview["total_deposited"]:.2f} USDT</code> ({overview["deposits_count"]} paid)"""


async def _show_admin_user_card(q, target):
    overview = get_user_overview(target)
    if not overview:
        await edit_or_send(q, f"❌ <b>No user found with ID</b> <code>{target}</code>.", admin_menu())
        return
    await edit_or_send(q, _admin_user_card_text(overview), admin_user_card_keyboard(target))


async def _show_admin_user_orders(q, target, page):
    total = count_search_orders(user_id=target)
    pages = max(1, math.ceil(total / ADMIN_USER_PAGE_SIZE))
    page = max(0, min(int(page), pages - 1))
    rows = search_orders(user_id=target, limit=ADMIN_USER_PAGE_SIZE, offset=page * ADMIN_USER_PAGE_SIZE)

    text = (
        f"📦 <b>User Orders</b>\n\n"
        f"🆔 User: <code>{target}</code>\n"
        f"Total: <code>{total}</code> order(s) | Page <code>{page + 1}/{pages}</code>\n\n"
    )
    kb = []
    if not rows:
        text += "No orders found for this user."
    for oid, user_id, product_name, price, created, method_stored, product_id, username, first_name in rows:
        text += (
            f"🧾 <b>#{oid}</b> — {_admin_name(product_name)} — <code>${float(price):.2f}</code>\n"
            f"🕒 <code>{display_time(created) or created}</code>\n\n"
        )
        kb.append([InlineKeyboardButton(f"🧾 Order #{oid} details", callback_data=f"admin_order_detail_{oid}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"admin_usr_orders_{target}_{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("▶️ Next", callback_data=f"admin_usr_orders_{target}_{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("⬅️ Back to User", callback_data=f"admin_usrcard_{target}")])
    await edit_or_send(q, text, InlineKeyboardMarkup(kb))


async def _show_admin_user_deposits(q, target, page):
    rows, total = get_user_deposits_page(target, ADMIN_USER_PAGE_SIZE, max(0, int(page)) * ADMIN_USER_PAGE_SIZE)
    pages = max(1, math.ceil(total / ADMIN_USER_PAGE_SIZE))
    if int(page) > pages - 1:
        page = pages - 1
        rows, total = get_user_deposits_page(target, ADMIN_USER_PAGE_SIZE, page * ADMIN_USER_PAGE_SIZE)
    page = max(0, int(page))

    text = (
        f"💰 <b>User Deposits</b>\n\n"
        f"🆔 User: <code>{target}</code>\n"
        f"Total: <code>{total}</code> deposit(s) | Page <code>{page + 1}/{pages}</code>\n\n"
    )
    if not rows:
        text += "No deposits found for this user."
    for dep_id, amount, method, status, created in rows:
        icon = "✅" if status == "paid" else "🟠" if status == "pending" else "🚫"
        mlabel = payment_method_label(method) if method else "USDT BEP20"
        text += (
            f"{icon} <b>#{dep_id}</b> — <code>{float(amount):.2f} USDT</code> — {mlabel}\n"
            f"🕒 <code>{display_time(created) or created}</code>\n\n"
        )
    kb = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"admin_usr_deps_{target}_{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("▶️ Next", callback_data=f"admin_usr_deps_{target}_{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("⬅️ Back to User", callback_data=f"admin_usrcard_{target}")])
    await edit_or_send(q, text, InlineKeyboardMarkup(kb))


async def _show_admin_user_stats(q, target):
    overview = get_user_overview(target)
    if not overview:
        await edit_or_send(q, "❌ <b>User not found.</b>", admin_menu())
        return
    cur.execute("SELECT MIN(created), MAX(created) FROM orders WHERE user_id=?", (target,))
    row = cur.fetchone() or (None, None)
    first_order, last_order = row
    avg = overview["total_spent"] / overview["orders_count"] if overview["orders_count"] else 0.0
    text = f"""📊 <b>User Statistics</b>

🆔 <b>User ID:</b> <code>{target}</code>

🛒 <b>Total Orders:</b> <code>{overview["orders_count"]}</code>
💸 <b>Total Spent:</b> <code>${overview["total_spent"]:.2f}</code>
📈 <b>Average Order:</b> <code>${avg:.2f}</code>
📥 <b>Total Deposited:</b> <code>${overview["total_deposited"]:.2f}</code> ({overview["deposits_count"]} paid)
🎁 <b>Referral Earned:</b> <code>{overview["referral_earnings"]:.6f} USDT</code>
⏳ <b>Referral Pending:</b> <code>{overview["referral_pending"]:.6f} USDT</code>
⭐ <b>Loyalty Points:</b> <code>{overview["loyalty_points"]:g}</code>
🏆 <b>VIP Rank:</b> {overview["vip_rank"]}
🕒 <b>First Order:</b> <code>{display_time(first_order) or "—"}</code>
🕒 <b>Last Order:</b> <code>{display_time(last_order) or "—"}</code>"""
    kb = [[InlineKeyboardButton("⬅️ Back to User", callback_data=f"admin_usrcard_{target}")]]
    await edit_or_send(q, text, InlineKeyboardMarkup(kb))


def price_offers_keyboard(product_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add / Update Offer", callback_data=f"admin_offer_add_{product_id}")],
        [InlineKeyboardButton("🗑 Delete Offer", callback_data=f"admin_offer_delete_{product_id}")],
        [InlineKeyboardButton("🧹 Clear All Offers", callback_data=f"admin_offer_clear_{product_id}")],
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"admin_offer_select_{product_id}")],
        [InlineKeyboardButton("⬅️ Choose Product", callback_data="admin_price_offers")],
        [InlineKeyboardButton("🏠 Products Menu", callback_data="admin_section_products")],
    ])


def get_price_offers_text(product_id: int):
    cur.execute("SELECT name, price FROM products WHERE id=?", (product_id,))
    product = cur.fetchone()
    if not product:
        return None

    name, base_price = product
    tiers = get_product_price_tiers(product_id)

    msg = f"""🏷 <b>Price Offers</b>

📦 <b>Product:</b> {name}
🆔 <b>Product ID:</b> <code>{product_id}</code>
💰 <b>Base Price:</b> <code>${base_price:.2f}</code>

━━━━━━━━━━━━━━
"""

    if not tiers:
        msg += "\nNo custom offers set.\n\nDefault buttons will be shown:\n• 1 / 2 / 5 / 10 at normal price."
    else:
        for tid, qty, price in tiers:
            msg += f"\n⚡ <b>{qty} for ${price:.2f}</b>"

    msg += "\n\nChoose an option below:"
    return msg


def add_product_skip_keyboard(cancel_callback: str = "admin_section_products"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Skip", callback_data="admin_add_skip")],
        [InlineKeyboardButton("❌ Cancel", callback_data=cancel_callback)],
    ])



def add_product_stock_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Skip Stock", callback_data="admin_add_stock_skip")],
        [InlineKeyboardButton("❌ Cancel", callback_data="admin_add_cancel")],
    ])


def add_product_photo_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Skip Photo", callback_data="admin_add_photo_skip")],
        [InlineKeyboardButton("❌ Cancel", callback_data="admin_section_products")],
    ])


def add_product_preview_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Publish Product", callback_data="admin_add_publish")],
        [InlineKeyboardButton("✏️ Edit Name", callback_data="admin_add_edit_name"), InlineKeyboardButton("💰 Edit Price", callback_data="admin_add_edit_price")],
        [InlineKeyboardButton("📝 Edit Description", callback_data="admin_add_edit_description")],
        [InlineKeyboardButton("📌 Edit Validity", callback_data="admin_add_edit_validity"), InlineKeyboardButton("🛡 Edit Warranty", callback_data="admin_add_edit_warranty")],
        [InlineKeyboardButton("📜 Edit Rules", callback_data="admin_add_edit_rules")],
        [InlineKeyboardButton("🖼 Change Photo", callback_data="admin_add_edit_photo"), InlineKeyboardButton("🔑 Edit Stock", callback_data="admin_add_edit_stock")],
        [InlineKeyboardButton("❌ Cancel", callback_data="admin_add_cancel")],
    ])


def add_product_preview_text(uid: int):
    data = admin_temp_data.get(uid, {})
    name = data.get("name", "Not set")
    price = float(data.get("price", 0) or 0)
    description = data.get("description", "No description")
    validity = data.get("validity", "Not mentioned")
    warranty = data.get("warranty", "No warranty")
    rules = data.get("rules", "No rules mentioned")
    stock_items = data.get("stock_items", [])
    photo = "✅ Added" if data.get("image_file_id") else "⏭ No photo"

    return f"""👀 <b>Product Preview</b>

📦 <b>Name:</b> {name}
💰 <b>Price:</b> <code>${price:.2f}</code>
📊 <b>Stock:</b> <code>{len(stock_items)}</code>
🖼 <b>Photo:</b> {photo}

📝 <b>Description:</b>
{description}

📌 <b>Validity:</b> {validity}
<b>Warranty:</b> {warranty}

<b>Rules:</b>
{rules}

Choose what you want to do:"""


def product_codes_keyboard(product_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Codes", callback_data=f"admin_codes_add_{product_id}")],
        [InlineKeyboardButton("🗑 Delete Codes", callback_data=f"admin_codes_delete_{product_id}")],
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"admin_codes_select_{product_id}")],
        [InlineKeyboardButton("⬅️ Choose Product", callback_data="admin_manage_codes")],
        [InlineKeyboardButton("🏠 Products Menu", callback_data="admin_section_products")],
    ])


def get_product_codes_text(product_id: int, limit: int = 80):
    cur.execute("SELECT name FROM products WHERE id=?", (product_id,))
    product = cur.fetchone()
    if not product:
        return None

    product_name = product[0]

    cur.execute("""
        SELECT id, delivery, sold, sold_at
        FROM product_stock
        WHERE product_id=?
        ORDER BY sold ASC, id DESC
        LIMIT ?
    """, (product_id, limit))
    rows = cur.fetchall()

    available = get_available_stock_count(product_id)
    sold_count = get_sold_stock_count(product_id)
    total = get_total_stock_count(product_id)

    msg = f"""<b>Product Codes Manager</b>

📦 <b>Product:</b> {_admin_name(product_name)}
🆔 <b>Product ID:</b> <code>{product_id}</code>

✅ <b>Available:</b> <code>{available}</code>
🔴 <b>Sold:</b> <code>{sold_count}</code>
📊 <b>Total:</b> <code>{total}</code>

━━━━━━━━━━━━━━
"""

    if not rows:
        msg += "\nNo codes added for this product yet."
        return msg

    for sid, delivery, sold, sold_at in rows:
        delivery = decrypt_text(delivery)
        status = "🔴 SOLD" if sold else "✅ AVAILABLE"
        msg += f"\n<b>Stock ID:</b> <code>{sid}</code> | {status}\n"
        msg += f"<code>{delivery}</code>\n"
        if sold_at:
            msg += f"🕒 Sold: <code>{sold_at}</code>\n"

        if len(msg) > 3700:
            msg += "\n... Showing first codes only. Use delete by Stock ID."
            break

    msg += "\n\nTo delete codes, tap <b>Delete Codes</b> and send Stock IDs."
    return msg


def products_select_keyboard(prefix: str, back_callback: str = "admin_section_products", limit: int = 40):
    cur.execute("SELECT id, name, price, stock, active FROM products ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    kb = []
    for pid, name, price, stock, active in rows:
        status = "🟢" if active else "🔴"
        kb.append([InlineKeyboardButton(f"{status} #{pid} • {strip_premium(name)[:35]}", callback_data=f"{prefix}_{pid}")])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data=back_callback)])
    return InlineKeyboardMarkup(kb)

def product_edit_options_keyboard(product_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Name", callback_data=f"admin_edit_field_{product_id}_name"), InlineKeyboardButton("💰 Price", callback_data=f"admin_edit_field_{product_id}_price")],
        [InlineKeyboardButton("📝 Description", callback_data=f"admin_edit_field_{product_id}_description")],
        [InlineKeyboardButton("📌 Validity", callback_data=f"admin_edit_field_{product_id}_validity"), InlineKeyboardButton("🛡 Warranty", callback_data=f"admin_edit_field_{product_id}_warranty")],
        [InlineKeyboardButton("📜 Rules", callback_data=f"admin_edit_field_{product_id}_rules")],
        [InlineKeyboardButton("🖼 Edit Photo", callback_data=f"admin_edit_field_{product_id}_photo"), InlineKeyboardButton("🗑 Remove Photo", callback_data=f"admin_edit_field_{product_id}_remove_photo")],
        [InlineKeyboardButton("⬅️ Choose Another Product", callback_data="admin_edit_product")],
        [InlineKeyboardButton("🏠 Products Menu", callback_data="admin_section_products")],
    ])

def get_product_preview(product_id: int):
    cur.execute("""
        SELECT id, name, price, stock, description, validity, warranty, rules, redeem_link, max_per_order, active
        FROM products WHERE id=?
    """, (product_id,))
    p = cur.fetchone()
    if not p:
        return None
    pid, name, price, stock, description, validity, warranty, rules, redeem_link, max_per_order, active = p
    status = "🟢 Active" if active else "🔴 Hidden"
    return f"""✏️ <b>Edit Product</b>

🆔 <b>ID:</b> <code>{pid}</code>
📦 <b>Name:</b> {name}
💰 <b>Price:</b> <code>${price:.2f}</code>
📊 <b>Stock:</b> <code>{stock}</code>
🚦 <b>Status:</b> {status}

📝 <b>Description:</b>
{description or 'No description'}

📌 <b>Validity:</b> {validity or 'Not set'}
🛡 <b>Warranty:</b> {warranty or 'No warranty'}
Choose what you want to edit:"""

# Temporary per-admin buffer for the "Add Login" wizard (email/password/2FA).


async def broadcast_product_update(context, product_id: int, product_name: str, price: float, added: int, available: int, mode: str = "stock"):
    """Broadcast new product/new stock alert to all users."""
    try:
        # Never show 'Available: 0' when the product actually has stock
        # (e.g. manual-stock products with no uploaded codes).
        try:
            if not available or int(available) <= 0:
                available = get_display_stock(product_id)
        except Exception:
            pass
        cur.execute("SELECT user_id FROM users WHERE COALESCE(blocked,0)=0 AND COALESCE(notifications,1)=1")
        users = cur.fetchall()

        if mode == "product":
            tpl_key = "new_product_broadcast"
            default_msg = "🆕 <b>New Product Added!</b>\n\n📦 <b>{product}</b>\n➕ <b>Added:</b> <code>{added}</code>\n✅ <b>Available:</b> <code>{available}</code>\n💰 <b>Price:</b> <code>{price}</code>\n\n<i>⚡ New product is now available!</i>"
        else:
            tpl_key = "stock_added_broadcast"
            default_msg = "🆕 <b>New Stock Added!</b>\n\n📦 <b>{product}</b>\n➕ <b>Added:</b> <code>{added}</code>\n✅ <b>Available:</b> <code>{available}</code>\n💰 <b>Price:</b> <code>{price}</code>\n\n<i>⚡ Grab yours before they’re gone!</i>"

        msg = render_message_template(
            tpl_key,
            default_msg,
            product_name=premium_safe(product_name),
            added=added,
            available=available,
            price=f"${float(price or 0):.2f}",
        )

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Tap to Buy Now", callback_data=f"product_{product_id}")]
        ])

        queued = _queue_broadcast(context, users, msg, kb, "AUTO_PRODUCT_BROADCAST", f"product={product_id} mode={mode}")
        return queued, 0

    except Exception:
        return 0, 0


async def broadcast_new_offer(context, product_id: int):
    """Broadcast a bulk-discount / price-offer alert to all users."""
    try:
        cur.execute("SELECT name, price FROM products WHERE id=?", (product_id,))
        row = cur.fetchone()
        if not row:
            return 0, 0
        product_name, price = row

        tiers = get_product_price_tiers(product_id)
        if not tiers:
            return 0, 0

        available = get_display_stock(product_id)

        # Show exactly what the buy flow charges for each quantity (same math
        # as calculate_total_price), so the alert can never advertise stale
        # or contradictory prices.
        base = float(price or 0)
        offer_lines = []
        for _tid, qty, tier_price in tiers:
            qty = int(qty)
            best_unit = base if base > 0 else (float(tier_price) / qty if qty else float(tier_price))
            for _t2, q2, p2 in tiers:
                q2 = int(q2)
                if 0 < q2 <= qty:
                    unit = float(p2) / q2
                    if unit < best_unit:
                        best_unit = unit
            shown_total = round(best_unit * qty, 2)
            each = shown_total / qty if qty else shown_total
            offer_lines.append(f"• <b>{qty}</b> for <b>${shown_total:.2f}</b> (${each:.2f} each)")
        offers_block = "\n".join(offer_lines)

        msg = render_message_template(
            "new_offer_broadcast",
            "🔥 <b>NEW OFFER ALERT!</b>\n\n📦 <b>{product}</b>\n💰 <b>Price:</b> <code>{price}</code> / code\n✅ <b>Available:</b> <code>{available}</code>\n\n🎁 <b>BULK DISCOUNT ACTIVE</b>\n{offers}\n\n⚡ <i>Grab this deal before it’s gone!</i>",
            product_name=_admin_name(product_name),
            price=f"${base:.2f}",
            available=available,
            offers=offers_block,
        )

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Tap to Buy Now", callback_data=f"product_{product_id}")]
        ])

        cur.execute("SELECT user_id FROM users WHERE COALESCE(blocked,0)=0 AND COALESCE(notifications,1)=1")
        users = cur.fetchall()

        queued = _queue_broadcast(context, users, msg, kb, "AUTO_OFFER_BROADCAST", f"product={product_id}")
        return queued, 0

    except Exception:
        return 0, 0


async def broadcast_price_change(context, product_id: int, product_name: str, old_price: float, new_price: float, available: int):
    """Broadcast a price change alert to all users."""
    try:
        # Always recompute the stock shown to customers so a stale/zero
        # caller value can never appear in the announcement.
        try:
            fresh = get_display_stock(product_id)
            if fresh > 0 or not available:
                available = fresh
        except Exception:
            pass
        cur.execute("SELECT user_id FROM users WHERE COALESCE(blocked,0)=0 AND COALESCE(notifications,1)=1")
        users = cur.fetchall()

        if new_price < old_price:
            pct = round((old_price - new_price) / old_price * 100) if old_price > 0 else 0
            tpl_key = "price_down_broadcast"
        else:
            pct = round((new_price - old_price) / old_price * 100) if old_price > 0 else 0
            tpl_key = "price_up_broadcast"

        bar = "\u2501" * 18

        # Out-of-stock products: don't push "Buy Now" — offer a restock alert instead.
        try:
            in_stock = int(available or 0) > 0
        except Exception:
            in_stock = False
        if in_stock:
            tail_line = "\U0001f6cd <i>Tap below to grab it now!</i> \U0001f447"
            action_button = InlineKeyboardButton("\U0001f6d2 Buy Now", callback_data=f"product_{product_id}")
        else:
            tail_line = "\u23f3 <i>Out of stock \u2014 restocking soon!</i> \U0001f514"
            action_button = InlineKeyboardButton("\U0001f514 Notify Me When Back", callback_data=f"notify_stock_{product_id}")

        from database.db import PRICE_UP_BROADCAST_DEFAULT, PRICE_DOWN_BROADCAST_DEFAULT
        msg = render_message_template(
            tpl_key,
            PRICE_DOWN_BROADCAST_DEFAULT if tpl_key == "price_down_broadcast" else PRICE_UP_BROADCAST_DEFAULT,
            product_name=_admin_name(product_name),
            old_price=f"${old_price:.2f}",
            new_price=f"${new_price:.2f}",
            pct=pct,
            stock=available,
            bar=bar,
            tail=tail_line,
        )

        kb = InlineKeyboardMarkup([[action_button]])

        queued = _queue_broadcast(context, users, msg, kb, "AUTO_PRICE_BROADCAST", f"product={product_id} {old_price}->{new_price}")
        return queued, 0
    except Exception:
        return 0, 0


admin_action_cooldowns = {}
DANGEROUS_ADMIN_COOLDOWN_SECONDS = 3


def admin_action_allowed(uid, action):
    now = time.time()
    key = f"{uid}:{action}"
    last = admin_action_cooldowns.get(key, 0)
    if now - last < DANGEROUS_ADMIN_COOLDOWN_SECONDS:
        return False, int(DANGEROUS_ADMIN_COOLDOWN_SECONDS - (now - last)) + 1
    admin_action_cooldowns[key] = now
    return True, 0


def _parse_ids(raw_text: str):
    clean = raw_text.replace(",", " ")
    return [int(x) for x in clean.split() if x.strip().isdigit()]


def _csv_bytes(headers, rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    return io.BytesIO(output.getvalue().encode("utf-8"))


async def _send_csv(update, filename, headers, rows):
    bio = _csv_bytes(headers, rows)
    bio.name = filename
    await update.message.reply_document(document=bio, filename=filename)


def _safe_money(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0



async def show_admin_panel(q, uid):
    if not is_admin(uid):
        await edit_or_send(
            q,
            "❌ <b>Admin only.</b>\n\n<i>You do not have permission to access this panel.</i>",
            back_home()
        )
        return

    cur.execute("SELECT COUNT(*) FROM users")
    users_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM products WHERE active=1")
    products_count = cur.fetchone()[0]
    reset_at = _revenue_reset_at()
    cur.execute("SELECT COUNT(*) FROM orders WHERE created >= ?", (reset_at,))
    orders_count = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(price),0) FROM orders WHERE created >= ?", (reset_at,))
    sales = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM product_stock WHERE sold=0")
    available_stock = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM tickets WHERE status='open'")
    open_tickets = cur.fetchone()[0]

    cur.execute("""
        SELECT product_name, price, created
        FROM orders
        ORDER BY id DESC
        LIMIT 3
    """)
    recent_orders = cur.fetchall()

    recent_text = ""
    if recent_orders:
        for product_name, price, created in recent_orders:
            recent_text += f"• {product_name} — <code>{price:.2f} USDT</code>\n"
    else:
        recent_text = "No recent orders yet.\n"

    await edit_or_send(
        q,
        f"""👑 <b>Admin Dashboard</b>

💵 <b>Revenue:</b> <code>{sales:.2f} USDT</code>
🛒 <b>Orders:</b> <code>{orders_count}</code>
👥 <b>Users:</b> <code>{users_count}</code>
📦 <b>Active Products:</b> <code>{products_count}</code>
<b>Available Stock:</b> <code>{available_stock}</code>
🎫 <b>Open Tickets:</b> <code>{open_tickets}</code>

<b>Recent Orders</b>
{recent_text}
Use the buttons below to manage your store.""",
        admin_menu()
    )


# ===================== Categories management (in-bot admin) =====================
# Same categories table the shop + web admin use. Per-admin state is kept in
# user_state[uid] / admin_temp_data[uid] so concurrent admins never clash.

def admin_categories_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Categories", callback_data="admin_cat_add")],
        [InlineKeyboardButton("✏️ Edit Categories", callback_data="admin_cat_edit")],
        [InlineKeyboardButton("📦 Add Existing Product", callback_data="admin_cat_pick:addex")],
        [InlineKeyboardButton("➕ Add New Product", callback_data="admin_cat_pick:addnew")],
        [InlineKeyboardButton("🗑️ Delete Category", callback_data="admin_cat_pick:del")],
        [InlineKeyboardButton("◀️ Back", callback_data="admin_panel")],
    ])


def _cat_cancel_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_cat_cancel")]])


def _cat_back_kb(cb="admin_categories"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data=cb)]])


def _cat_done_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Done", callback_data="admin_cat_done"),
        InlineKeyboardButton("❌ Cancel", callback_data="admin_cat_cancel"),
    ]])


def _category_picker_kb(action):
    """Inline category buttons for a follow-up action: edit / del / addex / addnew."""
    rows = []
    for cid, cname, _sort, _active, cicon in get_categories():
        _ptext, _picon = button_premium(cname)
        _cic = (cicon or "").strip()
        # Category names render EXACTLY as the admin entered them (no auto icon).
        rows.append([InlineKeyboardButton(
            f"{_cic + ' ' if _cic else ''}{_ptext}",
            callback_data=f"admin_cat_pickdo:{action}:{cid}",
            icon_custom_emoji_id=_picon
        )])
    rows.append([InlineKeyboardButton("◀️ Back", callback_data="admin_categories")])
    return InlineKeyboardMarkup(rows)


def _cat_products_count(cid):
    cur.execute("SELECT COUNT(*) FROM products WHERE category_id=?", (int(cid),))
    r = cur.fetchone()
    return int(r[0] or 0) if r else 0


def _find_product_for_category(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    cur.execute("SELECT id, name, category_id FROM products WHERE id=?", (pid,))
    return cur.fetchone()


def _cat_name_html(name):
    """Name for HTML messages: premium-emoji (<tg-emoji>) names render as-is;
    plain names are HTML-escaped. Names are sanitized at capture time."""
    s = str(name or "")
    if "<tg-emoji" in s:
        return s
    return html.escape(strip_premium(s))


def _products_id_list_text(limit=40):
    """Live Product ID -> name cheat sheet for the category flows. Always
    queried fresh, so newly added products appear automatically."""
    cur.execute("SELECT COUNT(*) FROM products")
    total = int(cur.fetchone()[0] or 0)
    cur.execute("SELECT id, name FROM products ORDER BY id ASC LIMIT ?", (int(limit),))
    rows = cur.fetchall()
    if not rows:
        return "<i>No products yet.</i>"
    lines = [f"<code>{r[0]}</code> — {_cat_name_html(r[1])}" for r in rows]
    if total > len(rows):
        lines.append(f"<i>…and {total - len(rows)} more — just send its Product ID.</i>")
    return "\n".join(lines)


def _cat_added_list_text(uid):
    added = admin_temp_data.get(uid, {}).get("cat_added", [])
    return "\n".join(
        f"{i}. 🆔 <code>{pid}</code> — {_cat_name_html(pname)}"
        for i, (pid, pname) in enumerate(added, 1)
    )


async def _show_categories_admin(q):
    await edit_or_send(
        q,
        """📂 <b>Categories Management</b>

Organize the 🛍 shop into categories.
Products without a category appear under 📦 <b>Other</b>.""",
        admin_categories_menu(),
    )


async def _show_category_detail(q, cid):
    cat = get_category(cid)
    if not cat:
        await edit_or_send(q, "❌ <b>Category not found.</b>", _cat_back_kb())
        return
    count = _cat_products_count(cid)
    await edit_or_send(
        q,
        f"""📂 <b>Category: {_cat_name_html(cat[1])}</b>

📦 <b>Products:</b> <code>{count}</code>

What would you like to edit?""",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Rename Category", callback_data=f"admin_cat_rename:{cid}")],
            [InlineKeyboardButton("📦 Manage Products", callback_data=f"admin_cat_prods:{cid}")],
            [InlineKeyboardButton("🗑️ Delete Category", callback_data=f"admin_cat_del:{cid}")],
            [InlineKeyboardButton("◀️ Back", callback_data="admin_cat_edit")],
        ]),
    )


async def _show_category_products_admin(q, cid, notice=None):
    cat = get_category(cid)
    if not cat:
        await edit_or_send(q, "❌ <b>Category not found.</b>", _cat_back_kb())
        return
    cur.execute("SELECT id, name FROM products WHERE category_id=? ORDER BY id DESC LIMIT 25", (int(cid),))
    rows = cur.fetchall()
    if rows:
        listing = "\n".join(f"🆔 <code>{r[0]}</code> — {_cat_name_html(r[1])}" for r in rows)
        total = _cat_products_count(cid)
        if total > len(rows):
            listing += f"\n\n<i>…and {total - len(rows)} more.</i>"
    else:
        listing = "<i>No products in this category yet.</i>"
    text = (notice + "\n\n" if notice else "") + f"""📂 <b>{_cat_name_html(cat[1])}</b>

Products in this category:

{listing}"""
    await edit_or_send(
        q,
        text,
        InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Existing Product", callback_data=f"admin_cat_addex:{cid}")],
            [InlineKeyboardButton("➕ Add New Product", callback_data=f"admin_cat_addnew:{cid}")],
            [InlineKeyboardButton("➖ Remove Product", callback_data=f"admin_cat_rm:{cid}")],
            [InlineKeyboardButton("◀️ Back", callback_data=f"admin_cat_sel:{cid}")],
        ]),
    )


async def _admin_cat_prompt_add_existing(q, uid, cid):
    cat = get_category(cid)
    if not cat:
        await edit_or_send(q, "❌ <b>Category not found.</b>", _cat_back_kb())
        return
    user_state[uid] = "admin_cat_add_existing"
    admin_temp_data.setdefault(uid, {})["cat_addex_id"] = cid
    await edit_or_send(
        q,
        f"""📦 <b>Add Existing Product</b>

📂 Category: <b>{_cat_name_html(cat[1])}</b>

Send the Product ID — or several at once, separated by commas.

Examples:
<code>30</code>
<code>1,2,3,4,5</code>

📦 <b>Products:</b>
{_products_id_list_text()}""",
        _cat_cancel_kb(),
    )


async def _admin_cat_start_addnew(q, uid, cid):
    """Reuse the EXISTING Add Product wizard; the new product is auto-assigned
    to this category at publish time via pending_category_id."""
    cat = get_category(cid)
    if not cat:
        await edit_or_send(q, "❌ <b>Category not found.</b>", _cat_back_kb())
        return
    user_state[uid] = "admin_add_product_name"
    admin_temp_data[uid] = {"pending_category_id": cid}
    await edit_or_send(
        q,
        f"""📂 <b>Category: {_cat_name_html(cat[1])}</b>

➕ Creating new product...

Step 1/7

Send product name.

Example:
<code>LinkedIn Business Premium – 2 Months</code>""",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_add_cancel")]]),
    )


async def _admin_cat_delete_confirm(q, cid):
    cat = get_category(cid)
    if not cat:
        await edit_or_send(q, "❌ <b>Category not found.</b>", _cat_back_kb())
        return
    count = _cat_products_count(cid)
    await edit_or_send(
        q,
        f"""⚠️ <b>Delete Category?</b>

📂 {_cat_name_html(cat[1])}

Products inside:
<code>{count}</code>

Deleting this category will NOT delete the products.

The products will become:
📂 Uncategorized""",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑️ Yes, Delete", callback_data=f"admin_cat_delok:{cid}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"admin_cat_sel:{cid}")],
        ]),
    )


async def _admin_cat_handle_product_id(update, uid, text, cat, in_create_flow):
    """Validate typed Product ID(s) and assign them to `cat`.

    Accepts one ID or several at once ("30", "1,2,3,4,5", one per line...).
    Rules per ID: not found / already here / move-confirm (other category) /
    direct assign (uncategorized). Assigning NEVER duplicates the product row.
    """
    cid = int(cat[0])
    raw = (text or "").strip()
    tokens = [t for t in raw.replace("\n", " ").replace(",", " ").split() if t]
    if not tokens or not all(t.isdigit() for t in tokens):
        await update.message.reply_text(
            "❌ <b>Product not found.</b>\n\nSend numeric Product IDs — one, or several separated by commas.\n\nExample: <code>30</code> or <code>1,2,3,4,5</code>",
            parse_mode="HTML",
        )
        return

    added, already, notfound, need_move = [], [], [], []
    seen = set()
    for t in tokens:
        pid = int(t)
        if pid in seen:
            continue
        seen.add(pid)
        prow = _find_product_for_category(pid)
        if not prow:
            notfound.append(pid)
            continue
        _pid, pname, pcat = prow
        if pcat and int(pcat) == cid:
            already.append(pid)
        elif pcat:
            need_move.append((pid, pname, pcat))
        else:
            assign_product_category(pid, cid)
            added.append((pid, pname))

    if added and in_create_flow:
        admin_temp_data.setdefault(uid, {}).setdefault("cat_added", []).extend(added)

    parts = []
    if added:
        if in_create_flow:
            total_added = len(admin_temp_data.get(uid, {}).get("cat_added", []))
            parts.append(
                f"✅ <b>{len(added)} product{'s' if len(added) > 1 else ''} added successfully.</b>\n\n"
                f"📂 <b>Category:</b> {_cat_name_html(cat[1])}\n"
                f"📦 <b>Products:</b> <code>{total_added}</code>\n\n{_cat_added_list_text(uid)}"
            )
        else:
            names = "\n".join(f"🆔 <code>{p}</code> — {_cat_name_html(n)}" for p, n in added)
            parts.append(
                f"✅ <b>{len(added)} product{'s' if len(added) > 1 else ''} added successfully.</b>\n\n"
                f"📂 <b>Category:</b> {_cat_name_html(cat[1])}\n\n{names}"
            )
    if notfound:
        parts.append("❌ Not found: " + ", ".join(f"<code>{p}</code>" for p in notfound))
    if already:
        parts.append("⚠️ Already in this category: " + ", ".join(f"<code>{p}</code>" for p in already))

    if parts:
        if in_create_flow:
            parts.append("Send more Product IDs (one or many, e.g. <code>1,2,3,4,5</code>) — or press ✅ Done")
            await update.message.reply_text(
                "\n\n".join(parts), reply_markup=_cat_done_kb(), parse_mode="HTML"
            )
        else:
            user_state.pop(uid, None)
            await update.message.reply_text(
                "\n\n".join(parts),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📦 Manage this category", callback_data=f"admin_cat_prods:{cid}")],
                    [InlineKeyboardButton("◀️ Back", callback_data="admin_categories")],
                ]),
                parse_mode="HTML",
            )

    # Products living in OTHER categories need an explicit move confirmation.
    if need_move:
        admin_temp_data.setdefault(uid, {})["cat_move"] = {
            "pids": [p for p, _n, _c in need_move],
            "target": cid,
            "in_create": bool(in_create_flow),
        }
        lines = []
        for pid, pname, pcat in need_move:
            other = get_category(pcat)
            lines.append(
                f"• 🆔 <code>{pid}</code> — {_cat_name_html(pname)}  "
                f"(now: 📂 {_cat_name_html(other[1]) if other else 'other'})"
            )
        await update.message.reply_text(
            f"""⚠️ <b>{len(need_move)} product{'s' if len(need_move) > 1 else ''} already belong{'s' if len(need_move) == 1 else ''} to other categories:</b>

{chr(10).join(lines)}

Do you want to move {'them' if len(need_move) > 1 else 'it'} to:

📂 {_cat_name_html(cat[1])}?""",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Move Product" + ("s" if len(need_move) > 1 else ""), callback_data="admin_cat_moveall"),
                InlineKeyboardButton("❌ Cancel", callback_data="admin_cat_movecancel"),
            ]]),
            parse_mode="HTML",
        )


async def handle_admin_callback(q, uid, data, context):
    if not is_admin(uid):
        await edit_or_send(
            q,
            "❌ <b>Admin only.</b>\n\n<i>You do not have permission to access this panel.</i>",
            back_home()
        )
        return True

    if data in ("admin_dashboard", "admin_menu", "admin_panel"):
        await show_admin_panel(q, uid)
        return True

    if data == "admin_warranty":
        pending = get_pending_replacements(10)
        warr_on = str(get_setting("warranty_enabled", "1")).strip() == "1"
        w_hours = int(float(get_setting("warranty_hours", "24") or 24))

        kb = []
        text = "🛡 <b>Warranty Requests</b>\n\n"
        text += "Status: " + ("🟢 ON" if warr_on else "🔴 OFF") + f" | Window: <b>{w_hours}h</b>\n\n"

        if not pending:
            text += "No pending replacement requests. ✅"
        else:
            for rid, r_order_id, r_user_id, r_product_id, r_created in pending:
                text += f"🧾 <b>#{rid}</b> | Order <code>#{r_order_id}</code> | User <code>{r_user_id}</code>\n🕒 <code>{r_created}</code>\n\n"
                kb.append([
                    InlineKeyboardButton(f"✅ Approve #{rid}", callback_data=f"admin_warr_ok_{rid}"),
                    InlineKeyboardButton(f"❌ Deny #{rid}", callback_data=f"admin_warr_no_{rid}"),
                ])

        kb.append([InlineKeyboardButton("⏰ Set Window Hours", callback_data="admin_warr_hours"),
                   InlineKeyboardButton(("🔴 Disable" if warr_on else "🟢 Enable"), callback_data="admin_warr_toggle")])
        kb.append([InlineKeyboardButton("🚫 Warranty-Ban / Unban User", callback_data="admin_warr_banuser")])
        kb.append([InlineKeyboardButton("⬅️ Admin Home", callback_data="admin_panel")])

        await edit_or_send(q, text, InlineKeyboardMarkup(kb))
        return True

    if data == "admin_warr_toggle":
        current = str(get_setting("warranty_enabled", "1")).strip()
        set_setting("warranty_enabled", "0" if current == "1" else "1")
        await edit_or_send(q, "✅ <b>Warranty setting updated.</b>", InlineKeyboardMarkup([[InlineKeyboardButton("🛡 Back to Warranty", callback_data="admin_warranty")]]))
        return True

    if data == "admin_warr_hours":
        user_state[uid] = "admin_warr_hours"
        await edit_or_send(q, "⏰ <b>Warranty Window</b>\n\nSend the number of hours (example: <code>24</code>).", back_home())
        return True

    if data == "admin_warr_banuser":
        user_state[uid] = "admin_warr_banuser"
        await edit_or_send(q, "🚫 <b>Warranty Ban / Unban</b>\n\nSend the user ID.\n\nIf the user is already banned they will be unbanned, otherwise they will be banned from auto-replacement.", back_home())
        return True

    if data.startswith("admin_warr_ok_"):
        rid = int(data.rsplit("_", 1)[1])
        rep = get_replacement(rid)

        if not rep or rep[6] != "pending":
            await edit_or_send(q, "❌ <b>Request not found or already processed.</b>", back_home())
            return True

        _rid, r_order_id, r_user_id, r_product_id, _old_enc, _new_enc, _status = rep
        new_code = reserve_stock_item(r_product_id, r_user_id) if r_product_id else None

        if not new_code:
            await edit_or_send(q, f"❌ <b>No stock available</b> for this product.\n\nRestock first, then approve request <code>#{rid}</code> again from 🛡 Warranty Requests.", back_home())
            return True

        cur.execute("UPDATE products SET stock=stock-1 WHERE id=? AND stock > 0", (r_product_id,))
        conn.commit()
        set_replacement_status(rid, "approved", new_code)

        try:
            await context.bot.send_message(
                chat_id=r_user_id,
                text=(
                    "✅ <b>Replacement Approved</b>\n\n"
                    f"🧾 Order: <code>#{r_order_id}</code>\n\n"
                    f"🔑 <b>New Delivery:</b>\n<code>{html.escape(new_code, quote=False)}</code>"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass

        await edit_or_send(q, f"✅ <b>Replacement #{rid} approved</b> and delivered to user <code>{r_user_id}</code>.", InlineKeyboardMarkup([[InlineKeyboardButton("🛡 Back to Warranty", callback_data="admin_warranty")]]))
        return True

    if data.startswith("admin_warr_no_"):
        rid = int(data.rsplit("_", 1)[1])
        rep = get_replacement(rid)

        if not rep or rep[6] != "pending":
            await edit_or_send(q, "❌ <b>Request not found or already processed.</b>", back_home())
            return True

        set_replacement_status(rid, "denied")

        try:
            await context.bot.send_message(
                chat_id=rep[2],
                text=f"❌ <b>Replacement Denied</b>\n\n🧾 Order: <code>#{rep[1]}</code>\n\nIf you believe this is a mistake, please create a support ticket.",
                parse_mode="HTML"
            )
        except Exception:
            pass

        await edit_or_send(q, f"❌ <b>Replacement #{rid} denied.</b>", InlineKeyboardMarkup([[InlineKeyboardButton("🛡 Back to Warranty", callback_data="admin_warranty")]]))
        return True

    if data.startswith("admin_warr_ban_"):
        ban_target = int(data.rsplit("_", 1)[1])
        set_warranty_ban(ban_target, True)
        await edit_or_send(q, f"🚫 <b>User <code>{ban_target}</code> is now warranty-banned.</b>\n\nAll their future replacement claims will need your manual approval.", back_home())
        return True

    if data == "admin_gift":
        g_rows = list_gift_codes(15)
        text = "🎁 <b>Gift Codes</b>\n\n"
        if not g_rows:
            text += "No gift codes created yet."
        else:
            for g_code, g_amount, g_creator, g_redeemer, g_created in g_rows:
                status = ("✅ redeemed by <code>" + str(g_redeemer) + "</code>") if g_redeemer else "🟢 active"
                text += f"<code>{g_code}</code> | <code>{float(g_amount):.2f}</code> | " + status + "\n"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Create Free Gift Code", callback_data="admin_gift_new")],
            [InlineKeyboardButton("⬅️ Admin Home", callback_data="admin_panel")],
        ])
        await edit_or_send(q, text, kb)
        return True

    if data == "admin_gift_new":
        user_state[uid] = "admin_gift_amount"
        await edit_or_send(q, "➕ <b>Create Free Gift Code</b>\n\nSend the amount in USDT (example: <code>10</code>).\n\nThis is an admin gift — no balance is deducted.", back_home())
        return True


    if data == "admin_customize":
        kb = [[InlineKeyboardButton("✏️ Edit Welcome Message", callback_data="admin_edit_welcome")]]
        for key, default, _cb in MAIN_MENU_BUTTONS:
            current = (get_setting(key, "") or "").strip() or default
            kb.append([InlineKeyboardButton(f"🔘 {strip_premium(current)[:30]}", callback_data=f"admin_btn_edit:{key}")])
        kb.append([InlineKeyboardButton("♻️ Reset All Buttons", callback_data="admin_btn_reset")])
        kb.append([InlineKeyboardButton("⬅️ Admin Home", callback_data="admin_panel")])
        await edit_or_send(
            q,
            "🎨 <b>Customize Bot</b>\n\n"
            "Edit the welcome message, or tap a main-menu button below to rename it.\n\n"
            "💎 Premium emoji supported — the first premium emoji in a button label becomes the button icon.",
            InlineKeyboardMarkup(kb)
        )
        return True

    if data == "admin_edit_welcome":
        user_state[uid] = "admin_set_welcome"
        await edit_or_send(
            q,
            "✏️ <b>Edit Welcome Message</b>\n\n"
            "Send the new welcome message now. Premium emoji are supported.\n\n"
            "<b>Placeholders:</b>\n"
            "<code>{name}</code> — user's first name\n"
            "<code>{store}</code> — store name\n"
            "<code>{owner}</code>, <code>{group}</code>, <code>{channel}</code>\n\n"
            "Send <code>reset</code> to restore the default message.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="admin_customize")]])
        )
        return True

    if data.startswith("admin_btn_edit:"):
        key = data.split(":", 1)[1]
        defaults = {k: d for k, d, _ in MAIN_MENU_BUTTONS}
        if key in defaults:
            user_state[uid] = f"admin_set_btn:{key}"
            current = (get_setting(key, "") or "").strip() or defaults[key]
            await edit_or_send(
                q,
                f"🔘 <b>Edit Button</b>\n\n"
                f"Current: <b>{strip_premium(current)}</b>\n"
                f"Default: {defaults[key]}\n\n"
                "Send the new button label now (emoji + text). "
                "A premium emoji becomes the button icon.\n\n"
                "Send <code>reset</code> to restore the default.",
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="admin_customize")]])
            )
        return True

    if data == "admin_btn_reset":
        for key, _default, _cb in MAIN_MENU_BUTTONS:
            try:
                cur.execute("DELETE FROM settings WHERE key=?", (key,))
            except Exception:
                pass
        conn.commit()
        log_admin_action(uid, "RESET_BUTTONS", "Main menu buttons reset")
        await edit_or_send(q, "♻️ <b>All main menu buttons reset to defaults.</b>", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_customize")]]))
        return True

    if data == "admin_bundles":
        import html as _html
        bundles = get_all_bundles()
        text = "🎁 <b>Bundle Deals</b>\n\n"
        kb = []
        if not bundles:
            text += "No bundles created yet.\n\nCreate one to show a 🎁 Bundle Deals section in the shop."
        else:
            for bid, bname, bprice, active in bundles:
                status = "🟢" if active else "🔴"
                items = get_bundle_items(bid)
                items_text = ", ".join(f"{iqty}× {_html.escape(str(ipname), quote=False)}" for _ipid, iqty, ipname, _ipp in items) or "empty"
                text += f"{status} <b>#{bid} {_html.escape(str(bname), quote=False)}</b> — ${float(bprice):.2f}\n{items_text}\n\n"
                kb.append([InlineKeyboardButton(f"🗑 Delete #{bid}", callback_data=f"admin_bundle_del_{bid}")])
        kb.append([InlineKeyboardButton("➕ Create Bundle", callback_data="admin_bundle_add")])
        kb.append([InlineKeyboardButton("⬅️ Admin Home", callback_data="admin_panel")])
        await edit_or_send(q, text, InlineKeyboardMarkup(kb))
        return True

    if data == "admin_bundle_add":
        user_state[uid] = "admin_bundle_create"
        await edit_or_send(
            q,
            "🎁 <b>Create Bundle</b>\n\n"
            "Send bundle details in this format:\n\n"
            "<code>Bundle Name | 9.99 | 12:1, 15:2</code>\n\n"
            "• <b>9.99</b> = bundle price\n"
            "• <b>12:1</b> = product ID 12 × quantity 1\n"
            "• Separate products with commas",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_bundles")]])
        )
        return True

    if data.startswith("admin_bundle_del_"):
        del_bundle_id = int(data.split("_")[-1])
        delete_bundle(del_bundle_id)
        await edit_or_send(
            q,
            f"🗑 Bundle <b>#{del_bundle_id}</b> deleted.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back To Bundles", callback_data="admin_bundles")]])
        )
        return True

    if data == "admin_section_products":
        await edit_or_send(
            q,
            """📦 <b>Products & Stock</b>

Manage products, stock, images, featured products, and bulk product actions.

Choose an option below:""",
            admin_products_menu()
        )
        return True

    if data == "admin_section_orders_users":
        await edit_or_send(
            q,
            """🛒 <b>Orders & Users</b>

Manage orders, refunds, delivery resend, users, balance, and exports.

Choose an option below:""",
            admin_orders_users_menu()
        )
        return True

    if data == "admin_section_marketing":
        await edit_or_send(
            q,
            """��� <b>Marketing & Growth</b>

Broadcast messages, referral settings, featured products, and sales stats.

Choose an option below:""",
            admin_marketing_menu()
        )
        return True

    if data == "admin_section_system":
        await edit_or_send(
            q,
            """🛡 <b>Security & System</b>

Backups, logs, stock alerts, and system reports.

Choose an option below:""",
            admin_system_menu()
        )
        return True

    if data == "admin_section_support":
        await edit_or_send(
            q,
            """🎫 <b>Support & Admins</b>

Manage support tickets, withdrawal approvals, and admin users.

Choose an option below:""",
            admin_support_menu()
        )
        return True

    if data == "admin_quick_add_product":
        user_state[uid] = "admin_quick_add_product"
        await edit_or_send(
            q,
            """⚡ <b>Quick Add Product</b>

Send all product details in one message:

<code>name | price | stock_items | description | validity | warranty | rules | redeem_link</code>

Use <code>;;</code> between stock items.

Example:
<code>Netflix Premium | 2.50 | email1:pass1;;email2:pass2 | 30 days account | 30 days | Replacement if invalid | No password change | Not required | 1</code>""",
            admin_menu()
        )
        return True

    if data == "admin_search_product":
        user_state[uid] = "admin_search_product"
        await edit_or_send(
            q,
            """🔎 <b>Search Product</b>

Send product ID or keyword.

Examples:
<code>12</code>
<code>Netflix</code>
<code>Premium</code>""",
            admin_menu()
        )
        return True

    if data == "admin_bulk_add_stock":
        user_state[uid] = "admin_bulk_add_stock"
        await edit_or_send(
            q,
            """📦 <b>Bulk Add Stock</b>

Send in this format:

<code>PRODUCT_ID | item1;;item2;;item3</code>

Example:
<code>5 | email1:pass1;;email2:pass2;;email3:pass3</code>""",
            admin_menu()
        )
        return True

    if data == "admin_low_stock":
        cur.execute("SELECT id, name, price, stock, active FROM products ORDER BY stock ASC, id DESC")
        rows = cur.fetchall()
        text = "⚠️ <b>Low / Out Of Stock Products</b>\n\n"
        found = 0

        for pid, name, price, old_stock, active in rows:
            live_stock = get_available_stock_count(pid)
            stock = live_stock if live_stock > 0 else old_stock
            if stock <= 5:
                found += 1
                status = "🔴 OUT" if stock <= 0 else "🟡 LOW"
                active_text = "ON" if active else "HIDDEN"
                text += (
                    f"{status} | <b>ID:</b> <code>{pid}</code>\n"
                    f"📦 <b>{_admin_name(name)}</b>\n"
                    f"💰 <code>{price:.2f} USDT</code> | 📊 Stock: <code>{stock}</code> | {active_text}\n\n"
                )

        if found == 0:
            text += "✅ No low-stock products found."

        if len(text) > 3900:
            text = text[:3900] + "\n..."

        await edit_or_send(q, text, admin_menu())
        return True

    if data == "admin_duplicate_product":
        user_state[uid] = "admin_duplicate_product"
        await edit_or_send(
            q,
            """📋 <b>Duplicate Product</b>

Send product ID to duplicate.

Example:
<code>12</code>

This copies product details but creates it with <b>0 stock</b>.
Then use Add Stock to add new delivery codes.""",
            admin_menu()
        )
        return True

    if data == "admin_bulk_hide_products":
        user_state[uid] = "admin_bulk_hide_products"
        await edit_or_send(
            q,
            """🙈 <b>Bulk Hide Products</b>

Send product IDs separated by comma or space.

Example:
<code>12,15,18</code>

or:
<code>12 15 18</code>""",
            admin_menu()
        )
        return True

    if data == "admin_bulk_unhide_products":
        user_state[uid] = "admin_bulk_unhide_products"
        await edit_or_send(
            q,
            """👁 <b>Bulk Unhide Products</b>

Send product IDs separated by comma or space.

Example:
<code>12,15,18</code>""",
            admin_menu()
        )
        return True

    if data == "admin_bulk_delete_products":
        user_state[uid] = "admin_bulk_delete_products"
        await edit_or_send(
            q,
            """🗑 <b>Bulk Permanent Delete Products</b>

⚠️ This permanently deletes products and all stock rows.

Send IDs and confirmation word:

<code>12,15,18 DELETE</code>

or:
<code>12 15 18 DELETE</code>""",
            admin_menu()
        )
        return True

    if data == "admin_search_orders":
        await edit_or_send(
            q,
            """🔎 <b>Search Orders</b>

Choose what to search by:""",
            admin_order_search_menu()
        )
        return True

    if data == "admin_export_orders":
        cur.execute("SELECT id, user_id, product_name, price, delivery, created FROM orders ORDER BY id DESC")
        rows = [list(_r) for _r in cur.fetchall()]
        for _r in rows:
            _r[4] = decrypt_text(_r[4])
        bio = _csv_bytes(["order_id", "user_id", "product_name", "price", "delivery", "created"], rows)
        bio.name = "orders_export.csv"
        await q.message.reply_document(document=bio, filename="orders_export.csv", caption="📤 Orders export")
        log_admin_action(uid, "EXPORT_ORDERS", f"{len(rows)} rows")
        return True

    if data == "admin_export_users":
        cur.execute("SELECT user_id, first_name, username, balance, blocked, joined FROM users ORDER BY joined DESC")
        rows = cur.fetchall()
        bio = _csv_bytes(["user_id", "first_name", "username", "balance", "blocked", "joined"], rows)
        bio.name = "users_export.csv"
        await q.message.reply_document(document=bio, filename="users_export.csv", caption="📤 Users export")
        log_admin_action(uid, "EXPORT_USERS", f"{len(rows)} rows")
        return True

    if data == "admin_search_user":
        user_state[uid] = "admin_search_user"
        await edit_or_send(
            q,
            """🔎 <b>Search User</b>

Send a Telegram user ID (exact) or a username.

Examples:
<code>7499348138</code>
<code>@durov</code>

Users without a username can only be found by their Telegram user ID.""",
            admin_menu()
        )
        return True

    if data == "admin_user_orders":
        user_state[uid] = "admin_user_orders"
        await edit_or_send(
            q,
            """📜 <b>User Purchase History</b>

Send Telegram user ID.

Example:
<code>7499348138</code>""",
            admin_menu()
        )
        return True

    if data == "admin_resend_order":
        user_state[uid] = "admin_resend_order"
        await edit_or_send(
            q,
            """🔄 <b>Resend Delivery</b>

Send order ID to resend delivery to the original buyer.

Example:
<code>25</code>""",
            admin_menu()
        )
        return True

    if data == "admin_refund_order":
        user_state[uid] = "admin_refund_order"
        await edit_or_send(
            q,
            """💸 <b>Refund Order</b>

Send order ID and confirmation:

<code>ORDER_ID REFUND</code>

Example:
<code>25 REFUND</code>

This adds the order price back to the user's balance.""",
            admin_menu()
        )
        return True

    if data == "admin_remove_balance":
        user_state[uid] = "admin_remove_balance"
        await edit_or_send(
            q,
            """💸 <b>Remove Balance</b>

Send in this format:

<code>USER_ID AMOUNT</code>

Example:
<code>7499348138 5</code>""",
            admin_menu()
        )
        return True

    if data == "admin_sales_stats":
        current_time = now()
        today = current_time.strftime("%Y-%m-%d")
        week_ago = (current_time - timedelta(days=7)).strftime("%Y-%m-%d")
        month_ago = (current_time - timedelta(days=30)).strftime("%Y-%m-%d")

        reset_at = _revenue_reset_at()
        cur.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE created >= ?", (reset_at,))
        total_orders, total_sales = cur.fetchone()

        cur.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE DATE(created)=DATE(?) AND created >= ?", (today, reset_at))
        today_orders, today_sales = cur.fetchone()

        cur.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE DATE(created)>=DATE(?) AND created >= ?", (week_ago, reset_at))
        week_orders, week_sales = cur.fetchone()

        cur.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE DATE(created)>=DATE(?) AND created >= ?", (month_ago, reset_at))
        month_orders, month_sales = cur.fetchone()

        cur.execute("""
            SELECT product_name, COUNT(*) c, COALESCE(SUM(price),0) s
            FROM orders
            WHERE created >= ?
            GROUP BY product_name
            ORDER BY c DESC, s DESC
            LIMIT 5
        """, (reset_at,))
        top_products = cur.fetchall()

        cur.execute("""
            SELECT u.user_id, u.first_name, u.username, COUNT(o.id) c, COALESCE(SUM(o.price),0) s
            FROM orders o
            LEFT JOIN users u ON o.user_id=u.user_id
            WHERE o.created >= ?
            GROUP BY o.user_id, u.user_id, u.first_name, u.username
            ORDER BY s DESC
            LIMIT 5
        """, (reset_at,))
        top_buyers = cur.fetchall()

        text = f"""📈 <b>Sales Statistics</b>

💵 <b>Total Sales:</b> <code>{total_sales:.2f} USDT</code>
🛒 <b>Total Orders:</b> <code>{total_orders}</code>

📅 <b>Today:</b> <code>{today_sales:.2f} USDT</code> / <code>{today_orders}</code> orders
📆 <b>7 Days:</b> <code>{week_sales:.2f} USDT</code> / <code>{week_orders}</code> orders
🗓 <b>30 Days:</b> <code>{month_sales:.2f} USDT</code> / <code>{month_orders}</code> orders

🏆 <b>Top Products</b>
"""
        if top_products:
            for name, count, sales in top_products:
                text += f"• {name} — <code>{count}</code> orders / <code>{sales:.2f}</code>\n"
        else:
            text += "No product sales yet.\n"

        text += "\n👑 <b>Top Buyers</b>\n"
        if top_buyers:
            for user_id, first_name, username, count, sales in top_buyers:
                uname = f"@{username}" if username and username != "No username" else (first_name or "User")
                text += f"• {uname} (<code>{user_id}</code>) — <code>{sales:.2f}</code> / <code>{count}</code> orders\n"
        else:
            text += "No buyers yet."

        if len(text) > 3900:
            text = text[:3900] + "\n..."

        await edit_or_send(q, text, admin_menu())
        return True

    if data == "admin_db_backup":
        os.makedirs("backups", exist_ok=True)
        stamp = now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join("backups", f"shopbot_backup_{stamp}.db")
        shutil.copyfile(DB_PATH, backup_path)
        await q.message.reply_document(
            document=open(backup_path, "rb"),
            filename=os.path.basename(backup_path),
            caption="💾 Database backup created"
        )
        log_admin_action(uid, "DB_BACKUP", backup_path)
        return True

    if data == "admin_withdrawals":
        rows = get_pending_withdrawals(20)
        text = "💸 <b>Pending Withdrawal Requests</b>\n\n"

        if not rows:
            text += "✅ No pending withdrawal requests."
        else:
            for wid, user_id, first_name, username, amount, wallet, status, created in rows:
                uname = f"@{username}" if username and username != "No username" else "No username"
                text += (
                    f"🧾 <b>Request #{wid}</b>\n"
                    f"👤 {first_name or 'User'} / {uname}\n"
                    f"🆔 <code>{user_id}</code>\n"
                    f"💰 <b>Amount:</b> <code>{amount:.6f} USDT</code>\n"
                    f"👛 <b>Wallet:</b> <code>{wallet}</code>\n"
                    f"🕒 <code>{created}</code>\n\n"
                    f"Approve: <code>{wid} APPROVE</code>\n"
                    f"Reject: <code>{wid} REJECT reason</code>\n\n"
                )

            user_state[uid] = "admin_process_withdrawal"
            text += "Send approval/rejection command now."

        if len(text) > 3900:
            text = text[:3900] + "\n..."

        await edit_or_send(q, text, admin_menu())
        return True

    if data == "admin_referral_settings":
        percent = get_setting("referral_percent", "5")
        minimum = get_setting("min_withdrawal", "1")
        user_state[uid] = "admin_referral_settings"
        await edit_or_send(
            q,
            f"""🎁 <b>Referral Settings</b>

Current settings:
📊 <b>Referral Commission:</b> <code>{percent}%</code>
💸 <b>Minimum Withdrawal:</b> <code>{minimum} USDT</code>

Send new settings in this format:

<code>REFERRAL_PERCENT | MIN_WITHDRAWAL</code>

Example:
<code>5 | 1</code>""",
            admin_menu()
        )
        return True

    if data == "admin_featured_products":
        user_state[uid] = "admin_featured_products"
        await edit_or_send(
            q,
            """⭐ <b>Featured Products</b>

Send in this format:

<code>PRODUCT_ID ON</code>
or
<code>PRODUCT_ID OFF</code>

Example:
<code>12 ON</code>

Featured products show at the top of the shop.""",
            admin_menu()
        )
        return True

    if data == "admin_set_product_image":
        user_state[uid] = "admin_set_product_image"
        await edit_or_send(
            q,
            """🖼 <b>Set Product Image</b>

Send product ID first.

Example:
<code>12</code>

After that, send the image/photo you want to attach to the product.""",
            admin_menu()
        )
        return True

    if data == "admin_remove_product_image":
        user_state[uid] = "admin_remove_product_image"
        await edit_or_send(
            q,
            """🖼 <b>Remove Product Image</b>

Send product ID.

Example:
<code>12</code>""",
            admin_menu()
        )
        return True

    if data.startswith("admin_edit_select_"):
        product_id = int(data.split("_")[-1])
        preview = get_product_preview(product_id)
        if not preview:
            await edit_or_send(q, "❌ <b>Product not found.</b>", products_select_keyboard("admin_edit_select"))
            return True
        await edit_or_send(q, preview, product_edit_options_keyboard(product_id))
        return True

    if data.startswith("admin_edit_field_"):
        parts = data.split("_")
        product_id = int(parts[3])
        field = "_".join(parts[4:])
        cur.execute("SELECT name FROM products WHERE id=?", (product_id,))
        product = cur.fetchone()
        if not product:
            await edit_or_send(q, "❌ <b>Product not found.</b>", products_select_keyboard("admin_edit_select"))
            return True
        if field == "remove_photo":
            remove_product_image(product_id)
            log_admin_action(uid, "REMOVE_PRODUCT_IMAGE", f"Product #{product_id}")
            await edit_or_send(q, f"✅ <b>Product photo removed.</b>\n\n🆔 Product ID: <code>{product_id}</code>", product_edit_options_keyboard(product_id))
            return True
        if field == "photo":
            user_state[uid] = f"admin_easy_edit:{product_id}:photo"
            await edit_or_send(q, f"🖼 <b>Edit Product Photo</b>\n\n📦 Product: <b>{product[0]}</b>\n🆔 ID: <code>{product_id}</code>\n\nNow send the new photo/image.", product_edit_options_keyboard(product_id))
            return True
        titles = {"name":"product name","price":"price","description":"description","validity":"validity","warranty":"warranty","rules":"rules"}
        user_state[uid] = f"admin_easy_edit:{product_id}:{field}"
        await edit_or_send(q, f"✏️ <b>Edit {titles.get(field, field)}</b>\n\n📦 Product: <b>{product[0]}</b>\n🆔 ID: <code>{product_id}</code>\n\nSend the new value only.", product_edit_options_keyboard(product_id))
        return True

    if data.startswith("admin_stock_select_"):
        product_id = int(data.split("_")[-1])
        cur.execute("SELECT name, stock FROM products WHERE id=?", (product_id,))
        product = cur.fetchone()
        if not product:
            await edit_or_send(q, "❌ <b>Product not found.</b>", products_select_keyboard("admin_stock_select"))
            return True
        if product_id in get_m365_product_ids():
            user_state[uid] = f"admin_m365_set_slots:{product_id}"
            await edit_or_send(q, f"""🎟 <b>Set Family Slots</b>

📦 Product: <b>{product[0]}</b>
🆔 ID: <code>{product_id}</code>
📊 Current Stock: <code>{product[1]}</code>

This is a <b>Microsoft 365 auto-invite</b> product — it does <b>NOT</b> use email:pass codes.
The invite is sent automatically from your saved Microsoft session.

Send a number to <b>ADD</b> slots on top of the current stock (a Family plan allows up to 5 extra people per ID).

Example: <code>3</code> → adds 3 more slots
Or <code>=5</code> → sets the total to exactly 5""", products_select_keyboard("admin_stock_select"))
            return True
        user_state[uid] = f"admin_easy_add_stock:{product_id}"
        await edit_or_send(q, f"""➕ <b>Add Stock</b>

📦 Product: <b>{product[0]}</b>
🆔 ID: <code>{product_id}</code>
📊 Current Stock: <code>{product[1]}</code>

Now send codes/accounts only.

Send one item per line:

<code>email1:pass1
email2:pass2
email3:pass3</code>

Or separate using <code>;;</code>.

📎 <b>NEW:</b> You can also upload <b>.txt file(s)</b> right here — each file is saved as <b>one account</b>, so multi-line details (email + passwords + links) stay together. Select several files at once to add several accounts.""", products_select_keyboard("admin_stock_select"))
        return True

    if data == "admin_manage_codes":
        await edit_or_send(
            q,
            """🧾 <b>Manage Product Codes</b>

Choose a product to view, add, or permanently delete its codes:""",
            products_select_keyboard("admin_codes_select")
        )
        return True

    if data.startswith("admin_codes_select_"):
        product_id = int(data.split("_")[-1])
        msg = get_product_codes_text(product_id)
        if not msg:
            await edit_or_send(q, "❌ <b>Product not found.</b>", products_select_keyboard("admin_codes_select"))
            return True

        await edit_or_send(q, msg, product_codes_keyboard(product_id))
        return True

    if data.startswith("admin_codes_add_"):
        product_id = int(data.split("_")[-1])
        cur.execute("SELECT name FROM products WHERE id=?", (product_id,))
        product = cur.fetchone()
        if not product:
            await edit_or_send(q, "❌ <b>Product not found.</b>", products_select_keyboard("admin_codes_select"))
            return True

        user_state[uid] = f"admin_codes_add:{product_id}"
        await edit_or_send(
            q,
            f"""➕ <b>Add Codes</b>

📦 Product: <b>{product[0]}</b>
🆔 Product ID: <code>{product_id}</code>

Send codes/accounts only.

One item per line:
<code>email1:pass1
email2:pass2
email3:pass3</code>

Or separate using <code>;;</code>.""",
            product_codes_keyboard(product_id)
        )
        return True

    if data.startswith("admin_codes_delete_"):
        product_id = int(data.split("_")[-1])
        cur.execute("SELECT name FROM products WHERE id=?", (product_id,))
        product = cur.fetchone()
        if not product:
            await edit_or_send(q, "❌ <b>Product not found.</b>", products_select_keyboard("admin_codes_select"))
            return True

        user_state[uid] = f"admin_codes_delete:{product_id}"
        await edit_or_send(
            q,
            f"""🗑 <b>Permanently Delete Codes</b>

📦 Product: <b>{product[0]}</b>
🆔 Product ID: <code>{product_id}</code>

Send Stock IDs to delete, separated by comma or space.

Example:
<code>12,15,18 DELETE</code>

⚠️ Confirmation word <code>DELETE</code> is required.""",
            product_codes_keyboard(product_id)
        )
        return True

    if data == "admin_add_cancel":
        user_state.pop(uid, None)
        admin_temp_data.pop(uid, None)
        await edit_or_send(q, "❌ <b>Add Product cancelled.</b>", admin_products_menu())
        return True

    if data == "admin_add_skip":
        state = user_state.get(uid)
        if state == "admin_add_product_description":
            admin_temp_data.setdefault(uid, {})["description"] = "No description"
            user_state[uid] = "admin_add_product_validity"
            await edit_or_send(q, "📌 <b>Step 4/7</b>\n\nSend validity or skip.", add_product_skip_keyboard())
            return True
        if state == "admin_add_product_validity":
            admin_temp_data.setdefault(uid, {})["validity"] = "Not mentioned"
            user_state[uid] = "admin_add_product_warranty"
            await edit_or_send(q, "🛡 <b>Step 5/7</b>\n\nSend warranty or skip.", add_product_skip_keyboard())
            return True
        if state == "admin_add_product_warranty":
            admin_temp_data.setdefault(uid, {})["warranty"] = "No warranty"
            user_state[uid] = "admin_add_product_rules"
            await edit_or_send(q, "📜 <b>Step 6/7</b>\n\nSend rules/terms or skip.", add_product_skip_keyboard())
            return True
        if state == "admin_add_product_rules":
            admin_temp_data.setdefault(uid, {})["rules"] = "No rules mentioned"
            user_state[uid] = "admin_add_product_photo"
            await edit_or_send(q, "🖼 <b>Step 7/7</b>\n\nSend product photo or skip.", add_product_photo_keyboard())
            return True

    if data == "admin_add_photo_skip":
        admin_temp_data.setdefault(uid, {})["image_file_id"] = None
        user_state[uid] = "admin_add_product_stock"
        await edit_or_send(
            q,
            """🔑 <b>Final Step</b>

Send stock codes/accounts, or tap <b>Skip Stock</b>.

One item per line:
<code>email1:pass1
email2:pass2
email3:pass3</code>

Or separate using <code>;;</code>.""",
            add_product_stock_keyboard()
        )
        return True

    if data == "admin_add_stock_skip":
        admin_temp_data.setdefault(uid, {})["stock_items"] = []
        user_state[uid] = "admin_add_product_preview"
        await edit_or_send(
            q,
            add_product_preview_text(uid),
            add_product_preview_keyboard()
        )
        return True

    if data.startswith("admin_add_edit_"):
        field = data.replace("admin_add_edit_", "")
        field_map = {
            "name": ("admin_add_product_name", "✏️ Send new product name."),
            "price": ("admin_add_product_price", "💰 Send new price."),
            "description": ("admin_add_product_description", "📝 Send new description or skip."),
            "validity": ("admin_add_product_validity", "📌 Send new validity or skip."),
            "warranty": ("admin_add_product_warranty", "🛡 Send new warranty or skip."),
            "rules": ("admin_add_product_rules", "📜 Send new rules or skip."),
            "photo": ("admin_add_product_photo", "🖼 Send new photo or skip."),
            "stock": ("admin_add_product_stock", "🔑 Send new stock codes/accounts."),
        }
        if field in field_map:
            user_state[uid] = field_map[field][0]
            kb = add_product_photo_keyboard() if field == "photo" else add_product_stock_keyboard() if field == "stock" else add_product_skip_keyboard() if field in ("description","validity","warranty","rules") else InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_add_cancel")]])
            await edit_or_send(q, field_map[field][1], kb)
            return True

    if data == "admin_add_publish":
        try:
            datax = admin_temp_data.get(uid, {})
            name = datax["name"]
            price = float(datax["price"])
            stock_items = datax.get("stock_items", [])
            delivery = ";;".join(stock_items)
            stock = len(stock_items)
            description = datax.get("description", "No description")
            validity = datax.get("validity", "Not mentioned")
            warranty = datax.get("warranty", "No warranty")
            rules = datax.get("rules", "No rules mentioned")
            redeem_link = "Not required"
            max_per_order = 999

            cur.execute(
                """
                INSERT INTO products(name, price, stock, delivery, description, validity, warranty, rules, redeem_link, max_per_order, active)
                VALUES(?,?,?,?,?,?,?,?,?,?,1)
                """,
                (name, price, stock, delivery, description, validity, warranty, rules, redeem_link, max_per_order)
            )
            conn.commit()

            product_id = cur.lastrowid
            added_items = add_stock_items(product_id, delivery) if delivery else 0
            cur.execute("UPDATE products SET stock=? WHERE id=?", (added_items, product_id))
            if datax.get("image_file_id"):
                cur.execute("UPDATE products SET image_file_id=? WHERE id=?", (datax.get("image_file_id"), product_id))
            # Categories: auto-assign when the wizard was started from a
            # category's "Add New Product" (pending_category_id is seeded then).
            pending_cat_name = None
            pending_cat = datax.get("pending_category_id")
            if pending_cat:
                _pc = get_category(pending_cat)
                if _pc:
                    cur.execute("UPDATE products SET category_id=? WHERE id=?", (int(pending_cat), product_id))
                    pending_cat_name = strip_premium(_pc[1])
            conn.commit()

            log_admin_action(uid, "ADD_PRODUCT_WIZARD", f"Product #{product_id} | {name} | stock {added_items}")

            try:
                sent, failed = await broadcast_product_update(context, product_id, name, price, added_items, added_items, mode="product")
            except Exception:
                sent, failed = 0, 0

            user_state.pop(uid, None)
            admin_temp_data.pop(uid, None)

            cat_line = f"📂 <b>Category:</b> {html.escape(pending_cat_name)}\n" if pending_cat_name else ""
            await edit_or_send(
                q,
                f"""✅ <b>Product Published</b>

🆔 <b>ID:</b> <code>{product_id}</code>
📦 <b>Name:</b> {name}
💰 <b>Price:</b> <code>${price:.2f}</code>
📊 <b>Stock:</b> <code>{added_items}</code>
{cat_line}📢 <b>Broadcast:</b> sent to <code>{sent}</code> users""",
                admin_products_menu()
            )
            return True

        except Exception as exc:
            await edit_or_send(q, f"❌ <b>Publish failed.</b>\n\n<code>{exc}</code>", add_product_preview_keyboard())
            return True

    if data == "admin_price_offers":
        await edit_or_send(
            q,
            """🏷 <b>Price Offers</b>

Choose a product to set quantity-based offer prices.

Example:
<code>5 | 4</code> means customer gets 5 items for $4.""",
            products_select_keyboard("admin_offer_select")
        )
        return True

    if data.startswith("admin_offer_select_"):
        product_id = int(data.split("_")[-1])
        msg = get_price_offers_text(product_id)
        if not msg:
            await edit_or_send(q, "❌ <b>Product not found.</b>", products_select_keyboard("admin_offer_select"))
            return True
        await edit_or_send(q, msg, price_offers_keyboard(product_id))
        return True

    if data.startswith("admin_offer_add_"):
        product_id = int(data.split("_")[-1])
        cur.execute("SELECT name FROM products WHERE id=?", (product_id,))
        product = cur.fetchone()
        if not product:
            await edit_or_send(q, "❌ <b>Product not found.</b>", products_select_keyboard("admin_offer_select"))
            return True

        user_state[uid] = f"admin_offer_add:{product_id}"
        await edit_or_send(
            q,
            f"""➕ <b>Add / Update Price Offer</b>

📦 Product: <b>{product[0]}</b>

Send one or multiple offers:

Single:
<code>5 | 4</code>

Multiple:
<code>1 | 1
5 | 4
10 | 7</code>

Format:
<code>QUANTITY | TOTAL_PRICE</code>""",
            price_offers_keyboard(product_id)
        )
        return True

    if data.startswith("admin_offer_delete_"):
        product_id = int(data.split("_")[-1])
        user_state[uid] = f"admin_offer_delete:{product_id}"
        await edit_or_send(
            q,
            """🗑 <b>Delete Price Offer</b>

Send quantity to delete.

Example:
<code>5</code>

This deletes the offer button for 5 items.""",
            price_offers_keyboard(product_id)
        )
        return True

    if data.startswith("admin_offer_clear_"):
        product_id = int(data.split("_")[-1])
        user_state[uid] = f"admin_offer_clear:{product_id}"
        await edit_or_send(
            q,
            """🧹 <b>Clear All Price Offers</b>

This removes all custom offers for this product.

Send:
<code>CLEAR</code>""",
            price_offers_keyboard(product_id)
        )
        return True

    if data.startswith("admin_order_detail_"):
        order_id = int(data.split("_")[-1])
        text, user_id, delivery = get_order_detail_text(order_id)
        if not text:
            await edit_or_send(q, "❌ <b>Order not found.</b>", admin_orders_list_keyboard())
            return True
        await edit_or_send(q, text, admin_order_detail_keyboard(order_id, user_id))
        return True

    if data.startswith("admin_order_resend_"):
        order_id = int(data.split("_")[-1])
        text, target_id, delivery = get_order_detail_text(order_id)
        if not text:
            await edit_or_send(q, "❌ <b>Order not found.</b>", admin_orders_list_keyboard())
            return True
        cur.execute("SELECT product_name, price FROM orders WHERE id=?", (order_id,))
        product_name, price = cur.fetchone()
        try:
            await q.message.get_bot().send_message(
                chat_id=target_id,
                text=("🔄 <b>Delivery Resent By Admin</b>\n\n"
                      f"🧾 <b>Order:</b> <code>#{order_id}</code>\n"
                      f"📦 <b>Product:</b> {_admin_name(product_name)}\n"
                      f"💰 <b>Paid:</b> <code>${price:.2f}</code>\n\n"
                      f"<b>Your Delivery</b>\n<code>{delivery}</code>"),
                parse_mode="HTML"
            )
            log_admin_action(uid, "ORDER_DETAIL_RESEND", f"Order #{order_id} to user {target_id}")
            await edit_or_send(q, f"✅ <b>Delivery resent.</b>\n\n🧾 Order: <code>#{order_id}</code>", admin_order_detail_keyboard(order_id, target_id))
        except Exception as exc:
            await edit_or_send(q, f"❌ <b>Resend failed.</b>\n\n<code>{exc}</code>", admin_order_detail_keyboard(order_id, target_id))
        return True

    if data.startswith("admin_order_refund_"):
        order_id = int(data.split("_")[-1])
        text, target_id, delivery = get_order_detail_text(order_id)
        if not text:
            await edit_or_send(q, "❌ <b>Order not found.</b>", admin_orders_list_keyboard())
            return True
        user_state[uid] = f"admin_order_refund_confirm:{order_id}"
        await edit_or_send(q, f"💸 <b>Refund Order Confirmation</b>\n\n🧾 Order: <code>#{order_id}</code>\n\nSend:\n<code>REFUND</code>", admin_order_detail_keyboard(order_id, target_id))
        return True

    if data.startswith("admin_order_user_"):
        target_id = int(data.split("_")[-1])
        # One canonical user view: the searchable user card (with Orders /
        # Deposits / Statistics buttons) instead of a static text dump.
        await _show_admin_user_card(q, target_id)
        return True

    if data.startswith("admin_order_delete_"):
        order_id = int(data.split("_")[-1])
        text, target_id, delivery = get_order_detail_text(order_id)
        if not text:
            await edit_or_send(q, "❌ <b>Order not found.</b>", admin_orders_list_keyboard())
            return True
        user_state[uid] = f"admin_order_delete_confirm:{order_id}"
        await edit_or_send(q, f"🗑 <b>Delete Order Log</b>\n\n🧾 Order: <code>#{order_id}</code>\n\nSend:\n<code>DELETE</code>\n\n⚠️ This only deletes order history.", admin_order_detail_keyboard(order_id, target_id))
        return True

    if data == "admin_flash_sales":
        await edit_or_send(
            q,
            """🔥 <b>Flash Sale Manager</b>

Choose a product to set or stop flash sale pricing:""",
            products_select_keyboard("admin_flash_select")
        )
        return True

    if data.startswith("admin_flash_select_"):
        product_id = int(data.split("_")[-1])
        msg = flash_sale_text(product_id)
        if not msg:
            await edit_or_send(q, "❌ <b>Product not found.</b>", products_select_keyboard("admin_flash_select"))
            return True
        await edit_or_send(q, msg, flash_sale_keyboard(product_id))
        return True

    if data.startswith("admin_flash_set_"):
        product_id = int(data.split("_")[-1])
        cur.execute("SELECT name, price FROM products WHERE id=?", (product_id,))
        p = cur.fetchone()
        if not p:
            await edit_or_send(q, "❌ <b>Product not found.</b>", products_select_keyboard("admin_flash_select"))
            return True
        user_state[uid] = f"admin_flash_set:{product_id}"
        await edit_or_send(
            q,
            f"""🔥 <b>Set Flash Sale</b>

📦 Product: <b>{p[0]}</b>
💰 Current Price: <code>${float(p[1]):.2f}</code>

Send:
<code>FLASH_PRICE | HOURS</code>

Example:
<code>0.70 | 24</code>""",
            flash_sale_keyboard(product_id)
        )
        return True

    if data.startswith("admin_flash_stop_"):
        product_id = int(data.split("_")[-1])
        cur.execute("UPDATE products SET flash_price=NULL, flash_ends_at=NULL WHERE id=?", (product_id,))
        conn.commit()
        log_admin_action(uid, "FLASH_SALE_STOP", f"Product #{product_id}")
        await edit_or_send(q, "✅ <b>Flash sale stopped.</b>", flash_sale_keyboard(product_id))
        return True

    if data == "admin_revenue_reset":
        reset_at = get_setting("revenue_reset_at", "")
        last_text = f"\n\n🗓 <b>Last reset (UTC):</b> <code>{reset_at}</code>" if reset_at else ""
        await edit_or_send(
            q,
            "♻️ <b>Reset Revenue Statistics?</b>\n\n"
            "All revenue and order counters will restart from <b>0</b> right now.\n\n"
            "✅ Order history and delivered products are <b>NOT deleted</b> — "
            "statistics simply start counting again from this moment.\n"
            "↩️ You can restore all-time statistics anytime." + last_text,
            InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes — Reset Now", callback_data="admin_revenue_reset_yes")],
                [InlineKeyboardButton("↩️ Restore All-Time Stats", callback_data="admin_revenue_reset_undo")],
                [InlineKeyboardButton("⬅️ Back", callback_data="admin_stats_dashboard")],
            ])
        )
        return True

    if data == "admin_revenue_reset_yes":
        stamp = _utc_now_text()
        set_setting("revenue_reset_at", stamp)
        log_admin_action(uid, "REVENUE_RESET", f"Revenue stats reset at {stamp} UTC")
        await edit_or_send(q, "✅ <b>Revenue reset done.</b> Statistics now count from this moment.\n\n" + admin_stats_text(), admin_stats_keyboard())
        return True

    if data == "admin_revenue_reset_undo":
        set_setting("revenue_reset_at", "")
        log_admin_action(uid, "REVENUE_RESET_UNDO", "Restored all-time revenue stats")
        await edit_or_send(q, "↩️ <b>All-time statistics restored.</b>\n\n" + admin_stats_text(), admin_stats_keyboard())
        return True

    if data == "admin_stats_dashboard":
        await edit_or_send(q, admin_stats_text(), admin_stats_keyboard())
        return True

    if data == "admin_search_order_menu":
        await edit_or_send(
            q,
            """🔎 <b>Search Orders</b>

Choose what to search by:""",
            admin_order_search_menu()
        )
        return True

    if data == "admin_coupons":
        await edit_or_send(q, "🎟 <b>Coupon Manager</b>\n\nCreate and manage discount coupons.", admin_coupons_keyboard())
        return True

    if data == "admin_coupon_create":
        user_state[uid] = "admin_coupon_create"
        await edit_or_send(
            q,
            """<b>Create Coupon</b>

Send:
<code>CODE | percent/fixed | value | max_uses | hours</code>

Examples:
<code>WELCOME10 | percent | 10 | 100 | 24</code>
<code>SAVE1 | fixed | 1 | 50 | 12</code>

Use max_uses <code>0</code> for unlimited.
Hours means coupon validity time before auto-expiry.""",
            admin_coupons_keyboard()
        )
        return True

    if data == "admin_reseller_coupon_create":
        user_state[uid] = "admin_reseller_coupon_create"
        await edit_or_send(
            q,
            """🤝 <b>Reseller Coupon</b>

Create a <b>private</b> coupon for a reseller. It will <b>NOT</b> be broadcast to users — share the code manually.

Send:
<code>CODE | percent/fixed | value | max_uses | hours</code>

Examples:
<code>RESELLER10 | percent | 10 | 100 | 720</code>
<code>VIP5 | fixed | 5 | 50 | 168</code>

Use max_uses <code>0</code> for unlimited.
Hours means coupon validity time before auto-expiry.""",
            admin_coupons_keyboard()
        )
        return True

    if data == "admin_coupon_list":
        cur.execute("SELECT code, discount_type, value, active, max_uses, used_count, expires_at, COALESCE(reseller,0) FROM coupons ORDER BY created DESC LIMIT 25")
        rows = cur.fetchall()
        text = "📋 <b>Coupons</b>\n\n"
        if not rows:
            text += "No coupons created yet."
        else:
            for code, dtype, value, active, max_uses, used, expires_at, reseller in rows:
                status = "✅" if active else "❌"
                limit = "∞" if not max_uses else str(max_uses)
                suffix = "%" if dtype == "percent" else " USDT"
                text += f"{status} <code>{code}</code>{' 🤝' if reseller else ''} — {value}{suffix} | Used <code>{used}/{limit}</code> | Expires <code>{expires_at or 'No expiry'}</code>\n"
        await edit_or_send(q, text, admin_coupons_keyboard())
        return True

    if data == "admin_coupon_delete":
        user_state[uid] = "admin_coupon_delete"
        await edit_or_send(q, "🗑 <b>Delete Coupon</b>\n\nSend coupon code to delete.", admin_coupons_keyboard())
        return True

    if data == "admin_product_analytics":
        await edit_or_send(q, "📈 <b>Product Analytics</b>\n\nChoose a product:", products_select_keyboard("admin_product_analytics_select"))
        return True

    if data.startswith("admin_product_analytics_select_"):
        product_id = int(data.split("_")[-1])
        msg = product_analytics_text(product_id)
        await edit_or_send(q, msg or "❌ Product not found.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Choose Product", callback_data="admin_product_analytics")]]))
        return True

    if data == "admin_top_sellers":
        await edit_or_send(q, top_sellers_text(), InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="admin_top_sellers")], [InlineKeyboardButton("⬅️ System", callback_data="admin_section_system")]]))
        return True

    if data == "admin_cleanup":
        await edit_or_send(q, cleanup_report_text(), admin_system_menu())
        return True

    if data == "admin_sales_settings":
        await edit_or_send(q, sales_settings_text(), admin_sales_settings_keyboard())
        return True

    if data == "admin_set_first_discount":
        user_state[uid] = "admin_set_first_discount"
        await edit_or_send(q, "🎉 Send first purchase discount percent.\n\nExample: <code>10</code>", admin_sales_settings_keyboard())
        return True

    if data == "admin_set_loyalty_percent":
        user_state[uid] = "admin_set_loyalty_percent"
        await edit_or_send(q, "💎 Send loyalty points percent.\n\nExample: <code>2</code>", admin_sales_settings_keyboard())
        return True

    if data == "admin_set_buyxgety":
        user_state[uid] = "admin_set_buyxgety"
        await edit_or_send(q, "🎁 Send Buy X Get Y format:\n\n<code>5 | 1</code>\n\nUse <code>0 | 0</code> to disable.", admin_sales_settings_keyboard())
        return True

    if data == "admin_ban_unban_menu":
        await edit_or_send(q, "🚫 <b>User Ban / Unban</b>\n\nChoose action:", user_management_keyboard())
        return True

    if data == "admin_ban_user":
        user_state[uid] = "admin_ban_user"
        await edit_or_send(q, "🚫 Send User ID to ban.", user_management_keyboard())
        return True

    if data == "admin_unban_user":
        user_state[uid] = "admin_unban_user"
        await edit_or_send(q, "✅ Send User ID to unban.", user_management_keyboard())
        return True

    if data == "admin_export_products":
        import csv, io
        cur.execute("SELECT id, name, price, stock, active, featured FROM products ORDER BY id DESC")
        rows = cur.fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "name", "price", "stock", "active", "featured"])
        writer.writerows(rows)
        data_file = io.BytesIO(output.getvalue().encode("utf-8"))
        data_file.name = "products_export.csv"
        await q.message.reply_document(document=data_file, caption="📤 Products export")
        return True

    if data == "admin_message_templates":
        await edit_or_send(
            q,
            """✏️ <b>Message Templates</b>

Choose any message to customize.

You can use HTML tags like:
<code>&lt;b&gt;bold&lt;/b&gt;</code>
<code>&lt;i&gt;italic&lt;/i&gt;</code>
<code>&lt;code&gt;copyable&lt;/code&gt;</code>""",
            message_templates_keyboard()
        )
        return True

    if data.startswith("admin_edit_template_"):
        key = data.replace("admin_edit_template_", "")
        cur.execute("SELECT title, body FROM message_templates WHERE key=?", (key,))
        row = cur.fetchone()
        if not row:
            await edit_or_send(q, "❌ Template not found.", message_templates_keyboard())
            return True

        title, body = row
        user_state[uid] = f"admin_edit_template:{key}"
        await edit_or_send(
            q,
            f"""✏️ <b>Edit {title}</b>

Send the exact message you want.

<b>Variables:</b>
<code>{template_help_text(key)}</code>

<b>Example:</b>
<code>{template_example_text(key)}</code>

<b>Current:</b>
<code>{body}</code>""",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📚 Variables", callback_data=f"admin_template_vars_{key}")],
                [InlineKeyboardButton("⬅️ Back", callback_data="admin_message_templates")]
            ])
        )
        return True

    if data == "admin_scheduled_broadcasts":
        await edit_or_send(q, "⏰ <b>Scheduled Broadcasts</b>\n\nCreate or view scheduled broadcasts.", scheduled_broadcast_keyboard())
        return True

    if data == "admin_schedule_broadcast":
        user_state[uid] = "admin_schedule_broadcast"
        await edit_or_send(
            q,
            """➕ <b>Schedule Broadcast</b>

Send:
<code>YYYY-MM-DD HH:MM | Message</code>

Example:
<code>2026-06-01 20:00 | 🔥 Flash sale starts now!</code>""",
            scheduled_broadcast_keyboard()
        )
        return True

    if data == "admin_view_scheduled_broadcasts":
        rows = get_scheduled_broadcasts(15)
        text = "📋 <b>Scheduled Broadcasts</b>\n\n"
        if not rows:
            text += "No scheduled broadcasts."
        else:
            for bid, msg, run_at, status, sent, failed in rows:
                text += f"#{bid} | <code>{run_at}</code> | <b>{status}</b> | sent {sent}/{failed}\n{msg[:80]}\n\n"
        await edit_or_send(q, text, scheduled_broadcast_keyboard())
        return True

    if data == "admin_sales_analytics":
        await edit_or_send(q, sales_analytics_text(), InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="admin_sales_analytics")], [InlineKeyboardButton("⬅️ System", callback_data="admin_section_system")]]))
        return True

    if data == "admin_smart_dashboard":
        s = smart_dashboard_stats()
        await edit_or_send(
            q,
            f"""📊 <b>Smart Admin Dashboard</b>

<b>Today</b>
💰 Revenue: <code>${s['today_revenue']:.2f}</code>
🛒 Orders: <code>{s['today_orders']}</code>
👥 New Users: <code>{s['new_users']}</code>

<b>Needs Attention</b>
🎫 Open Tickets: <code>{s['open_tickets']}</code>
⚠️ Low Stock Products: <code>{s['low_stock']}</code>

<b>All Time</b>
💵 Revenue: <code>${s['total_revenue']:.2f}</code>
🛒 Orders: <code>{s['total_orders']}</code>""",
            smart_dashboard_keyboard()
        )
        return True

    if data == "admin_ticket_manager":
        rows = get_open_tickets(20)
        text = "🎫 <b>Ticket Manager</b>\n\n"
        kb_rows = []
        if not rows:
            text += "No open tickets."
        else:
            for tid, user_id, first_name, username, subject, msg, status, created, updated in rows:
                title = subject or str(msg)[:30]
                kb_rows.append([InlineKeyboardButton(f"#{tid} • {status} • {str(title)[:30]}", callback_data=f"admin_ticket_view_{tid}")])
        kb_rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="admin_ticket_manager")])
        kb_rows.append([InlineKeyboardButton("⬅️ Support", callback_data="admin_section_support")])
        await edit_or_send(q, text, InlineKeyboardMarkup(kb_rows))
        return True

    if data.startswith("admin_ticket_view_"):
        ticket_id = int(data.split("_")[-1])
        t = get_ticket(ticket_id)
        if not t:
            await edit_or_send(q, "❌ Ticket not found.", ticket_manager_keyboard())
            return True
        tid, user_id, first_name, username, subject, msg, status, reply, created, updated = t
        username_text = f"@{username}" if username and username != "No username" else "No username"
        await edit_or_send(
            q,
            f"""🎫 <b>Ticket #{tid}</b>

👤 User: <code>{user_id}</code>
👤 Name: <b>{first_name or 'User'}</b> / <code>{username_text}</code>
Subject: <b>{subject or 'Support Ticket'}</b>
📌 Status: <b>{status}</b>

<b>User Message:</b>
{msg}

<b>Admin Reply:</b>
{reply or 'No reply yet'}""",
            ticket_manager_keyboard(tid, user_id)
        )
        return True

    if data.startswith("admin_ticket_reply_"):
        ticket_id = int(data.split("_")[-1])
        user_state[uid] = f"admin_ticket_reply:{ticket_id}"
        await edit_or_send(q, f"💬 Send reply for ticket <code>#{ticket_id}</code>.", ticket_manager_keyboard())
        return True

    if data.startswith("admin_ticket_close_"):
        ticket_id = int(data.split("_")[-1])
        close_ticket(ticket_id)
        await edit_or_send(q, f"✅ Ticket <code>#{ticket_id}</code> closed.", ticket_manager_keyboard())
        return True

    if data.startswith("admin_ticket_ban_"):
        target_id = int(data.split("_")[-1])
        block_user(target_id)
        await edit_or_send(q, f"🚫 User banned: <code>{target_id}</code>", ticket_manager_keyboard())
        return True

    if data == "admin_auto_marketing":
        await edit_or_send(q, auto_marketing_text(), auto_marketing_keyboard())
        return True

    if data == "admin_set_welcome_coupon":
        user_state[uid] = "admin_set_welcome_coupon"
        await edit_or_send(q, "🎁 Send welcome coupon code, or send <code>OFF</code> to disable.", auto_marketing_keyboard())
        return True

    if data == "admin_set_inactive_coupon":
        user_state[uid] = "admin_set_inactive_coupon"
        await edit_or_send(q, "😴 Send inactive-user coupon code, or send <code>OFF</code> to disable.", auto_marketing_keyboard())
        return True

    if data == "admin_set_inactive_days":
        user_state[uid] = "admin_set_inactive_days"
        await edit_or_send(q, "📅 Send number of inactive days.\n\nExample: <code>7</code>", auto_marketing_keyboard())
        return True

    if data == "admin_restore_backup":
        user_state[uid] = "admin_restore_backup"
        await edit_or_send(
            q,
            "📥 <b>Restore Backup</b>\n\nSend the backup ZIP file as a document.\n\n⚠️ This will replace the current database.",
            admin_system_menu()
        )
        return True

    if data == "admin_add_product":
        user_state[uid] = "admin_add_product_name"
        admin_temp_data[uid] = {}
        await edit_or_send(
            q,
            """➕ <b>Add Product</b>

Step 1/7

Send product name.

Example:
<code>LinkedIn Business Premium – 2 Months</code>""",
            InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_section_products")]])
        )
        return True


    if data == "admin_quick_add_product":
        user_state[uid] = "admin_quick_add_product"
        await edit_or_send(
            q,
            """⚡ <b>Quick Add Product</b>

Send all product details in one message:

<code>name | price | stock_items | description | validity | warranty | rules | redeem_link</code>

<b>Stock items:</b> use <code>;;</code> between each code/account.

Example:
<code>LinkedIn Premium 2 Months | 0.70 | code1;;code2;;code3 | 2 Months Premium Access | 2 Months | No warranty | Redeem ASAP | Not required | 1</code>

You can also use <code>-</code> for optional fields.""",
            admin_menu()
        )
        return True

    if data == "admin_permanent_delete_product":
        user_state[uid] = "admin_permanent_delete_product"
        await edit_or_send(
            q,
            """🗑 <b>Permanently Delete Product</b>

⚠️ This will delete:
• product
• all unsold/sold stock rows for that product

Send in this format:

<code>PRODUCT_ID DELETE</code>

Example:
<code>12 DELETE</code>""",
            admin_menu()
        )
        return True



    if data == "admin_edit_product":
        await edit_or_send(
            q,
            """✏️ <b>Edit Product</b>

Choose the product you want to edit:""",
            products_select_keyboard("admin_edit_select")
        )
        return True


    if data == "admin_delete_product":
        user_state[uid] = "admin_delete_product"
        await edit_or_send(
            q,
            """🗑 <b>Hide Product</b>

Send the product ID you want to hide.

Example:
<code>12</code>""",
            admin_menu()
        )
        return True

    if data == "admin_products":
        cur.execute("SELECT id, name, price, stock, active, validity, warranty, max_per_order FROM products ORDER BY id DESC")
        rows = cur.fetchall()

        text = "📦 <b>Product Management</b>\n\n🔎 Use Search Product for quick lookup.\n️ LOW means stock ≤ 5.\n\n"

        if not rows:
            text += "😕 <i>No products added yet.</i>"
        else:
            for pid, name, price, old_stock, active, validity, warranty, max_per_order in rows:
                live_stock = get_available_stock_count(pid)
                stock = live_stock if live_stock > 0 else old_stock
                status = "🟢 ON" if active else "🔴 OFF"
                stock_status = "🔴 OUT" if stock <= 0 else ("🟡 LOW" if stock <= 5 else "🟢 OK")
                text += (
                    f"🆔 <b>ID:</b> <code>{pid}</code>\n"
                    f"📦 <b>Name:</b> <code>{name}</code>\n"
                    f"💰 <b>Price:</b> <code>{price:.2f} USDT</code>\n"
                    f"📊 <b>Available Stock:</b> <code>{stock}</code> {stock_status}\n"
                    f"📌 <b>Validity:</b> <code>{validity}</code>\n"
                    f"🛡 <b>Warranty:</b> <code>{warranty}</code>\n"
                    f"🔢 <b>Max:</b> <code>{max_per_order}</code>\n"
                    f"🚦 <b>Status:</b> {status}\n"
                    f"\n"
                )

        if len(text) > 3900:
            text = text[:3900] + "\n..."

        await edit_or_send(q, text, admin_menu())
        return True


    if data == "admin_add_stock":
        await edit_or_send(
            q,
            """➕ <b>Add Stock</b>

Choose the product where you want to add stock:""",
            products_select_keyboard("admin_stock_select")
        )
        return True


    if data == "admin_inventory":
        cur.execute("SELECT id, name, price, active FROM products ORDER BY id DESC")
        rows = cur.fetchall()

        text = "📊 <b>Inventory / Stock</b>\n\n"

        if not rows:
            text += "😕 <i>No products found.</i>"
        else:
            for pid, name, price, active in rows:
                available = get_available_stock_count(pid)
                sold = get_sold_stock_count(pid)
                total = get_total_stock_count(pid)
                status = "🟢 ON" if active else "🔴 OFF"

                text += (
                    f"🆔 <b>ID:</b> <code>{pid}</code>\n"
                    f"📦 <b>Name:</b> <code>{name}</code>\n"
                    f"💰 <b>Price:</b> <code>{price:.2f} USDT</code>\n"
                    f"✅ <b>Available:</b> <code>{available}</code>\n"
                    f"🛒 <b>Sold:</b> <code>{sold}</code>\n"
                    f"📊 <b>Total Added:</b> <code>{total}</code>\n"
                    f"🚦 <b>Status:</b> {status}\n"
                    f"\n"
                )

        if len(text) > 3900:
            text = text[:3900] + "\n..."

        await edit_or_send(q, text, admin_menu())
        return True


    if data == "admin_add_balance":
        user_state[uid] = "admin_add_balance"
        await edit_or_send(
            q,
            """💰 <b>Add Balance</b>

Send in this format:

<code>USER_ID AMOUNT</code>

Example:
<code>7499348138 10</code>

This instantly adds wallet balance to the user.""",
            admin_menu()
        )
        return True

    if data == "admin_stats":
        cur.execute("SELECT COUNT(*) FROM users")
        users_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM products WHERE active=1")
        products_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM orders")
        orders_count = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(price),0) FROM orders")
        sales = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(balance),0) FROM users")
        total_balances = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM product_stock WHERE sold=0")
        available_stock = cur.fetchone()[0]

        await edit_or_send(
            q,
            f"""📈 <b>Store Stats</b>

👥 Users: <code>{users_count}</code>
📦 Active Products: <code>{products_count}</code>
📊 Available Stock: <code>{available_stock}</code>
🛒 Orders: <code>{orders_count}</code>
💵 Sales: <code>{sales:.2f} USDT</code>
👛 User Balances: <code>{total_balances:.2f} USDT</code>

Updated from local database.""",
            admin_menu()
        )
        return True

    if data == "admin_users":
        cur.execute("""
            SELECT user_id, first_name, username, balance, joined, blocked
            FROM users
            ORDER BY joined DESC
        """)
        rows = cur.fetchall()

        text = "👥 <b>Users</b>\n\n"

        if not rows:
            text += "😕 <i>No users found.</i>"
        else:
            for i, (user_id, first_name, username, balance, joined, blocked) in enumerate(rows, 1):
                username_text = f"@{username}" if username and username != "No username" else "No username"
                block_status = "🚫 BLOCKED" if blocked else "✅ ACTIVE"
                text += (
                    f"<b>{i}. {first_name or 'User'}</b>\n"
                    f"👤 <b>Username:</b> <code>{username_text}</code>\n"
                    f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
                    f"💰 <b>Balance:</b> <code>{balance:.2f} USDT</code>\n"
                    f"📅 <b>Joined:</b> <code>{joined}</code>\n"
                    f"🚦 <b>Status:</b> <code>{block_status}</code>\n"
                    f"\n"
                )

        if len(text) > 3900:
            text = text[:3900] + "\n..."

        await edit_or_send(q, text, admin_menu())
        return True


    if data == "admin_orders":
        cur.execute("SELECT COUNT(*) FROM orders")
        total_orders = cur.fetchone()[0]
        await edit_or_send(
            q,
            f"""🛒 <b>Orders</b>

Total Orders: <code>{total_orders}</code>

Choose an order to view details:""",
            admin_orders_list_keyboard()
        )
        return True


    if data == "admin_logs":
        rows = get_admin_logs(25)

        text = "🧾 <b>Admin Logs</b>\n\n"

        if not rows:
            text += "😕 <i>No admin logs found yet.</i>"
        else:
            for log_id, admin_id, action, details, created in rows:
                safe_details = details if details else "No details"
                if len(safe_details) > 120:
                    safe_details = safe_details[:120] + "..."

                text += (
                    f"🆔 <b>Log:</b> <code>#{log_id}</code>\n"
                    f"👑 <b>Admin:</b> <code>{admin_id}</code>\n"
                    f"⚙️ <b>Action:</b> <code>{action}</code>\n"
                    f"📝 <b>Details:</b> <code>{safe_details}</code>\n"
                    f"🕒 <b>Date:</b> <code>{created}</code>\n"
                    f"\n"
                )

        if len(text) > 3900:
            text = text[:3900] + "\n..."

        await edit_or_send(q, text, admin_menu())
        return True


    if data == "admin_tickets":
        rows = get_open_tickets(20)
        text = "🎫 <b>Open Tickets</b>\n\n"

        if not rows:
            text += "No open tickets."
        else:
            for ticket_id, user_id, first_name, username, subject, message, status, created, updated in rows:
                username_text = f"@{username}" if username and username != "No username" else "No username"
                short_msg = message[:120] + "..." if len(message) > 120 else message
                text += (
                    f"🎫 <b>Ticket #{ticket_id}</b>\n"
                    f"👤 {first_name or 'User'} / {username_text}\n"
                    f"<code>{user_id}</code>\n"
                    f"📌 <b>{subject}</b>\n"
                    f"📝 {short_msg}\n"
                    f"🕒 <code>{created}</code>\n"
                    f"Reply: <code>/replyticket {ticket_id} message</code>\n"
                    f"Close: <code>/closeticket {ticket_id}</code>\n\n"
                )

        if len(text) > 3900:
            text = text[:3900] + "\n..."

        await edit_or_send(q, text, admin_menu())
        return True

    if data == "admin_manage_admins":
        if not is_owner(uid):
            await edit_or_send(
                q,
                "❌ <b>Owner only.</b>\n\nOnly the main owner can manage admins.",
                admin_menu()
            )
            return True

        rows = get_admins()
        text = "🌍 <b>Admin Management</b>\n\n"
        text += "Commands:\n<code>/addadmin USER_ID</code>\n<code>/removeadmin USER_ID</code>\n\n"

        if not rows:
            text += "No admins found."
        else:
            for admin_id, first_name, username, added_by, added_at in rows:
                username_text = f"@{username}" if username and username != "No username" else "No username"
                role = "👑 Owner" if is_owner(admin_id) else "🛡 Admin"
                text += (
                    f"{role}\n"
                    f"🆔 <code>{admin_id}</code>\n"
                    f"👤 {first_name or 'User'} / {username_text}\n"
                    f"➕ Added by: <code>{added_by}</code>\n"
                    f"🕒 <code>{added_at}</code>\n\n"
                )

        await edit_or_send(q, text, admin_menu())
        return True

    # ---------------- Microsoft 365 Family Auto-Invite ----------------
    if data == "admin_m365":
        try:
            sess_rows = list_m365_sessions()
            sess_current = get_current_m365_session()
            if sess_current:
                session_line = f"✅ {len(sess_rows)} ID(s) stored | current: <b>{html.escape(str(sess_current[1]), quote=False)}</b>"
            elif sess_rows:
                session_line = "🔴 ALL IDs FULL or EXPIRED — upload a fresh session!"
            else:
                from services.m365_family import has_session
                session_line = "✅ Uploaded" if has_session() else "❌ Not uploaded yet"
        except Exception:
            session_line = "❌ Not uploaded yet"

        try:
            from database.db import count_queued_m365_invites
            _queued_ct = count_queued_m365_invites()
        except Exception:
            _queued_ct = 0
        queued_line = (
            f"\n🛟 <b>Queued orders:</b> <b>{_queued_ct}</b> waiting for a fresh session (auto-deliver)"
            if _queued_ct else ""
        )

        enabled_ids = get_m365_product_ids()
        products_line = ", ".join(f"#{p}" for p in sorted(enabled_ids)) if enabled_ids else "None yet"

        invite_lines = ""
        for inv in list_m365_invites(5):
            inv_id, inv_uid, inv_order_id, inv_email, inv_status, inv_error, inv_created = inv
            icon = "✅" if inv_status == "sent" else ("❌" if inv_status == "failed" else ("🛟" if inv_status == "queued" else "⏳"))
            invite_lines += f"\n{icon} <code>{html.escape(str(inv_email), quote=False)}</code> — {inv_status}"
        if not invite_lines:
            invite_lines = "\nNo invites yet."

        await edit_or_send(
            q,
            "📩 <b>Microsoft 365 Auto-Invite</b>\n\n"
            f"🔑 <b>Microsoft session:</b> {session_line}{queued_line}\n"
            f"📦 <b>Auto-invite products:</b> {products_line}\n"
            f"\n🧾 <b>Recent invites:</b>{invite_lines}\n\n"
            "⚙️ How it works: customer buys an enabled product → bot asks their email → "
            "bot opens your Microsoft account (saved session) and sends the Family invite automatically.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 Choose Products", callback_data="admin_m365_products")],
                [InlineKeyboardButton("🔑 Upload 6-Month ID", callback_data="admin_m365_upload_6m"), InlineKeyboardButton("🔑 Upload 1-Year ID", callback_data="admin_m365_upload_1y")],
                [InlineKeyboardButton("🔑 Upload Session (any)", callback_data="admin_m365_upload"), InlineKeyboardButton("🧪 Test Invite", callback_data="admin_m365_test")],
                [InlineKeyboardButton("📂 Sessions", callback_data="admin_m365_sessions")],
                [InlineKeyboardButton("🧾 All Invites", callback_data="admin_m365_invites")],
                [InlineKeyboardButton("⬅️ Admin Home", callback_data="admin_panel")],
            ])
        )
        return True

    if data in ("admin_m365_upload", "admin_m365_upload_6m", "admin_m365_upload_1y"):
        _w = "6m" if data.endswith("_6m") else ("1y" if data.endswith("_1y") else "")
        user_state[uid] = "admin_upload_m365_session" + ((":" + _w) if _w else "")
        _w_line = {
            "6m": "🏷 <b>This ID will serve: 6-MONTH WARRANTY invites</b>\n\n",
            "1y": "🏷 <b>This ID will serve: 1-YEAR WARRANTY invites</b>\n\n",
        }.get(_w, "🏷 <b>This ID will serve: ANY invite</b> (no warranty group)\n\n")
        await edit_or_send(
            q,
            "🔑 <b>Upload Microsoft Session</b>\n\n"
            + _w_line +
            "Do this ONCE on your own PC:\n\n"
            "1️⃣ <code>pip install playwright</code>\n"
            "2️⃣ <code>python -m playwright install chromium</code>\n"
            "3️⃣ <code>python tools/m365_login.py</code>\n"
            "4️⃣ Log in to your Microsoft account in the browser that opens — tick <b>Keep me signed in</b>\n"
            "5️⃣ Press ENTER in the terminal → it saves <code>m365_session.json</code>\n"
            "6️⃣ Send that file here as a document 📎\n\n"
            "⚠️ Keep that file private — it gives access to your Microsoft account. It is stored encrypted.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_m365")]])
        )
        return True

    if data == "admin_m365_test":
        user_state[uid] = "admin_m365_test_email"
        await edit_or_send(
            q,
            "🧪 <b>Test Invite</b>\n\n"
            "Send an email address — a REAL Microsoft 365 Family invite will be sent to it.\n\n"
            "⚠️ If accepted it uses one family slot, so test with your own second email.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_m365")]])
        )
        return True

    if data == "admin_m365_products" or data.startswith("admin_m365_toggle_") or data.startswith("admin_m365_warr_"):
        if data.startswith("admin_m365_toggle_"):
            try:
                toggle_m365_product(int(data.replace("admin_m365_toggle_", "")))
            except Exception:
                pass
        if data.startswith("admin_m365_warr_"):
            try:
                _wpid = int(data.replace("admin_m365_warr_", ""))
                _next = {"": "6m", "6m": "1y", "1y": ""}.get(get_m365_product_warranty(_wpid) or "", "6m")
                set_m365_product_warranty(_wpid, _next)
                log_admin_action(uid, "M365_PRODUCT_WARRANTY", f"Product #{_wpid} -> {_next or 'any'}")
            except Exception:
                pass

        enabled_ids = get_m365_product_ids()
        cur.execute("SELECT id, name FROM products WHERE active=1 ORDER BY id")
        m365_products = cur.fetchall()

        rows = []
        for prod_id, prod_name in m365_products[:40]:
            label_name = strip_premium(str(prod_name))[:35]
            mark = "✅" if prod_id in enabled_ids else "⬜"
            rows.append([InlineKeyboardButton(f"{mark} #{prod_id} {label_name}", callback_data=f"admin_m365_toggle_{prod_id}")])
            if prod_id in enabled_ids:
                _pw = get_m365_product_warranty(prod_id) or ""
                _pwl = {"6m": "6-Month IDs", "1y": "1-Year IDs"}.get(_pw, "Any ID")
                rows.append([InlineKeyboardButton(f"      🏷 sends from: {_pwl} — tap to change", callback_data=f"admin_m365_warr_{prod_id}")])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_m365")])

        await edit_or_send(
            q,
            "📦 <b>M365 Auto-Invite Products</b>\n\n"
            "Tap a product to switch auto-invite ON/OFF.\n\n"
            "✅ ON = after payment the bot asks the customer's email and sends the Microsoft 365 Family invite automatically (no stock codes used).\n"
            "⬜ OFF = normal stock-code delivery.\n\n"
            "💡 Tip: set the product's <b>stock</b> number = free family slots (a Family plan allows up to 5 extra people).",
            InlineKeyboardMarkup(rows)
        )
        return True

    if data == "admin_m365_invites":
        invites = list_m365_invites(15)
        text_out = "🧾 <b>M365 Invites</b>\n"
        rows = []

        for inv in invites:
            inv_id, inv_uid, inv_order_id, inv_email, inv_status, inv_error, inv_created = inv
            icon = "✅" if inv_status == "sent" else ("❌" if inv_status == "failed" else "⏳")
            text_out += (
                f"\n{icon} <b>#{inv_id}</b> <code>{html.escape(str(inv_email), quote=False)}</code>\n"
                f"👤 <code>{inv_uid}</code> | 🛒 Order #{inv_order_id} | {inv_status}"
            )
            if inv_error:
                text_out += f"\n⚠️ <code>{html.escape(str(inv_error)[:90], quote=False)}</code>"
            text_out += "\n"
            if inv_status == "failed":
                rows.append([InlineKeyboardButton(f"🔁 Retry #{inv_id}", callback_data=f"admin_m365_retry_{inv_id}")])

        if not invites:
            text_out += "\nNo invites yet."

        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_m365")])
        await edit_or_send(q, text_out, InlineKeyboardMarkup(rows))
        return True

    if data == "admin_m365_sessions":
        sess_rows = list_m365_sessions()
        try:
            per_id = int(float(get_setting("m365_invites_per_session", "5") or 5))
        except Exception:
            per_id = 5
        text = "📂 <b>Microsoft Sessions</b>\n\n"
        text += f"Each ID sends <b>{per_id}</b> family invites, then the bot rotates to the next ID automatically and alerts you.\n"
        text += "🏷 Tap 🏷 to set an ID's warranty group: Any → 6-Month → 1-Year.\n\n"
        kb = []
        if not sess_rows:
            text += "No sessions uploaded yet."
        else:
            sess_current = get_current_m365_session()
            current_id = sess_current[0] if sess_current else None
            for row in sess_rows:
                sid, sess_label, sess_used, sess_status = row[0], row[1], row[2], row[3]
                sess_w = str(row[6] or "") if len(row) > 6 else ""
                sess_w_tag = {"6m": " 🏷6M", "1y": " 🏷1Y"}.get(sess_w, "")
                if sess_status == "active":
                    icon = "🟢" if sid == current_id else "🕒"
                    marker = " ← current" if sid == current_id else " (queued)"
                elif sess_status == "expired":
                    icon = "🔴"
                    marker = " (EXPIRED — re-upload)"
                else:
                    icon = "🔴"
                    marker = " (FULL)"
                text += f"{icon} <b>#{sid}</b> {html.escape(str(sess_label), quote=False)}{sess_w_tag} — <code>{sess_used}/{per_id}</code>{marker}\n"
                kb.append([
                    InlineKeyboardButton(f"🏷 #{sid}", callback_data=f"admin_m365_sess_warr_{sid}"),
                    InlineKeyboardButton(f"🔄 Reset #{sid}", callback_data=f"admin_m365_sess_reset_{sid}"),
                    InlineKeyboardButton(f"🗑 Delete #{sid}", callback_data=f"admin_m365_sess_del_{sid}"),
                ])
        kb.append([InlineKeyboardButton("⏳ Invites Per ID", callback_data="admin_m365_sess_limit"), InlineKeyboardButton("🔑 Upload Session", callback_data="admin_m365_upload")])
        kb.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_m365")])
        await edit_or_send(q, text, InlineKeyboardMarkup(kb))
        return True

    if data.startswith("admin_m365_sess_warr_"):
        sess_id = int(data.replace("admin_m365_sess_warr_", ""))
        try:
            cur.execute("SELECT COALESCE(warranty,'') FROM m365_sessions WHERE id=?", (sess_id,))
            _row = cur.fetchone()
            _cur_w = str(_row[0] or "") if _row else ""
        except Exception:
            _cur_w = ""
        _next_w = {"": "6m", "6m": "1y", "1y": ""}.get(_cur_w, "6m")
        set_m365_session_warranty(sess_id, _next_w)
        log_admin_action(uid, "M365_SESSION_WARRANTY", f"session #{sess_id} -> {_next_w or 'any'}")
        _next_label = {"6m": "6-Month", "1y": "1-Year"}.get(_next_w, "Any (no group)")
        await edit_or_send(q, f"🏷 <b>Session #{sess_id} warranty group: {_next_label}</b>\n\nInvites for products tagged with this group are sent ONLY from matching IDs.", InlineKeyboardMarkup([[InlineKeyboardButton("📂 Back to Sessions", callback_data="admin_m365_sessions")]]))
        return True

    if data.startswith("admin_m365_sess_reset_"):
        sess_id = int(data.rsplit("_", 1)[1])
        reset_m365_session(sess_id)
        log_admin_action(uid, "M365_SESSION_RESET", f"session #{sess_id}")
        await edit_or_send(q, f"🔄 <b>Session #{sess_id} reset</b> — counter back to <code>0</code> and marked active.\n\nUse this after removing members from that Microsoft family.", InlineKeyboardMarkup([[InlineKeyboardButton("📂 Back to Sessions", callback_data="admin_m365_sessions")]]))
        return True

    if data.startswith("admin_m365_sess_del_"):
        sess_id = int(data.rsplit("_", 1)[1])
        delete_m365_session(sess_id)
        log_admin_action(uid, "M365_SESSION_DELETE", f"session #{sess_id}")
        await edit_or_send(q, f"🗑 <b>Session #{sess_id} deleted.</b>", InlineKeyboardMarkup([[InlineKeyboardButton("📂 Back to Sessions", callback_data="admin_m365_sessions")]]))
        return True

    if data == "admin_m365_sess_limit":
        user_state[uid] = "admin_m365_sess_limit"
        await edit_or_send(q, "⏳ <b>Invites Per ID</b>\n\nSend how many invites each Microsoft ID can send before the bot rotates to the next one.\n\nA Family plan allows <b>5</b> extra members.\n\nExample: <code>5</code>", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_m365_sessions")]]))
        return True

    if data.startswith("admin_m365_retry_"):
        try:
            retry_invite_id = int(data.replace("admin_m365_retry_", ""))
        except Exception:
            return True

        set_m365_invite_status(retry_invite_id, "pending", None)

        import asyncio
        from services.m365_family import process_invite
        asyncio.create_task(process_invite(q.message.get_bot(), retry_invite_id))

        await edit_or_send(
            q,
            f"🔁 <b>Retrying invite #{retry_invite_id}...</b>\n\nYou will get the result here within a minute.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_m365")]])
        )
        return True

    if data == "admin_canva":
        try:
            from services.canva_invite import has_session as canva_has_session
            session_line = "✅ Uploaded" if canva_has_session() else "❌ Not uploaded yet"
        except Exception:
            session_line = "❌ Not uploaded yet"

        enabled_ids = get_canva_product_ids()
        products_line = ", ".join(f"#{p}" for p in sorted(enabled_ids)) if enabled_ids else "None yet"

        invite_lines = ""
        for inv in list_canva_invites(5):
            inv_id, inv_uid, inv_order_id, inv_email, inv_status, inv_error, inv_created = inv
            icon = "✅" if inv_status == "sent" else ("❌" if inv_status == "failed" else "⏳")
            invite_lines += f"\n{icon} <code>{html.escape(str(inv_email), quote=False)}</code> — {inv_status}"
        if not invite_lines:
            invite_lines = "\nNo invites yet."

        await edit_or_send(
            q,
            "🎨 <b>Canva Auto-Invite</b>\n\n"
            f"🔑 <b>Canva session:</b> {session_line}\n"
            f"📦 <b>Auto-invite products:</b> {products_line}\n"
            f"\n🧾 <b>Recent invites:</b>{invite_lines}\n\n"
            "⚙️ How it works: customer buys an enabled product → bot asks their email → "
            "bot opens your Canva team (saved session), clicks Invite people, enters the email and sends the invitation automatically.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 Choose Products", callback_data="admin_canva_products")],
                [InlineKeyboardButton("🔑 Upload Session", callback_data="admin_canva_upload"), InlineKeyboardButton("🧪 Test Invite", callback_data="admin_canva_test")],
                [InlineKeyboardButton("🧾 All Invites", callback_data="admin_canva_invites")],
                [InlineKeyboardButton("⬅️ Admin Home", callback_data="admin_panel")],
            ])
        )
        return True

    if data == "admin_canva_upload":
        user_state[uid] = "admin_upload_canva_session"
        await edit_or_send(
            q,
            "🔑 <b>Upload Canva Session</b>\n\n"
            "Do this ONCE on your own PC:\n\n"
            "1️⃣ <code>pip install playwright</code>\n"
            "2️⃣ <code>python -m playwright install chromium</code>\n"
            "3️⃣ <code>python tools/canva_login.py</code>\n"
            "4️⃣ Log in to your Canva account in the browser that opens and wait for the People page\n"
            "5️⃣ Press ENTER in the terminal → it saves <code>canva_session.json</code>\n"
            "6️⃣ Send that file here as a document 📎\n\n"
            "⚠️ Keep that file private — it gives access to your Canva account. It is stored encrypted.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_canva")]])
        )
        return True

    if data == "admin_canva_test":
        user_state[uid] = "admin_canva_test_email"
        await edit_or_send(
            q,
            "🧪 <b>Test Invite</b>\n\n"
            "Send an email address — a REAL Canva team invitation will be sent to it.\n\n"
            "⚠️ If accepted it joins your team, so test with your own second email.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_canva")]])
        )
        return True

    if data == "admin_canva_products" or data.startswith("admin_canva_toggle_"):
        if data.startswith("admin_canva_toggle_"):
            try:
                toggle_canva_product(int(data.replace("admin_canva_toggle_", "")))
            except Exception:
                pass

        enabled_ids = get_canva_product_ids()
        cur.execute("SELECT id, name FROM products WHERE active=1 ORDER BY id")
        canva_products = cur.fetchall()

        rows = []
        for prod_id, prod_name in canva_products[:40]:
            label_name = strip_premium(str(prod_name))[:35]
            mark = "✅" if prod_id in enabled_ids else "⬜"
            rows.append([InlineKeyboardButton(f"{mark} #{prod_id} {label_name}", callback_data=f"admin_canva_toggle_{prod_id}")])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_canva")])

        await edit_or_send(
            q,
            "📦 <b>Canva Auto-Invite Products</b>\n\n"
            "Tap a product to switch auto-invite ON/OFF.\n\n"
            "✅ ON = after payment the bot asks the customer's email and sends the Canva team invitation automatically (no stock codes used).\n"
            "⬜ OFF = normal stock-code delivery.\n\n"
            "💡 Tip: set the product's <b>stock</b> number = free seats left in your Canva team.",
            InlineKeyboardMarkup(rows)
        )
        return True

    if data == "admin_canva_invites":
        invites = list_canva_invites(15)
        text_out = "🧾 <b>Canva Invites</b>\n"
        rows = []

        for inv in invites:
            inv_id, inv_uid, inv_order_id, inv_email, inv_status, inv_error, inv_created = inv
            icon = "✅" if inv_status == "sent" else ("❌" if inv_status == "failed" else "⏳")
            text_out += (
                f"\n{icon} <b>#{inv_id}</b> <code>{html.escape(str(inv_email), quote=False)}</code>\n"
                f"👤 <code>{inv_uid}</code> | 🛒 Order #{inv_order_id} | {inv_status}"
            )
            if inv_error:
                text_out += f"\n⚠️ <code>{html.escape(str(inv_error)[:90], quote=False)}</code>"
            text_out += "\n"
            if inv_status == "failed":
                rows.append([InlineKeyboardButton(f"🔁 Retry #{inv_id}", callback_data=f"admin_canva_retry_{inv_id}")])

        if not invites:
            text_out += "\nNo invites yet."

        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_canva")])
        await edit_or_send(q, text_out, InlineKeyboardMarkup(rows))
        return True

    if data.startswith("admin_canva_retry_"):
        try:
            retry_invite_id = int(data.replace("admin_canva_retry_", ""))
        except Exception:
            return True

        set_canva_invite_status(retry_invite_id, "pending", None)

        import asyncio
        from services.canva_invite import process_invite as canva_process_invite
        asyncio.create_task(canva_process_invite(q.message.get_bot(), retry_invite_id))

        await edit_or_send(
            q,
            f"🔁 <b>Retrying invite #{retry_invite_id}...</b>\n\nYou will get the result here within a minute.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_canva")]])
        )
        return True

    if data == "admin_broadcast":
        user_state[uid] = "admin_broadcast"
        await edit_or_send(
            q,
            """📢 <b>Broadcast</b>

Send the message you want to send to all users.

Use carefully. This will be sent to every saved user.""",
            admin_menu()
        )
        return True

    # ---------- Order search (paginated) ----------
    if data in ("admin_osearch_id", "admin_osearch_uid", "admin_osearch_user", "admin_osearch_date"):
        prompts = {
            "admin_osearch_id": ("admin_osearch:id", "🧾 <b>Search Order by ID</b>\n\nSend the order ID.\n\nExample: <code>123</code>"),
            "admin_osearch_uid": ("admin_osearch:uid", "👤 <b>Search Orders by User ID</b>\n\nSend the buyer's Telegram user ID.\n\nExample: <code>7499348138</code>"),
            "admin_osearch_user": ("admin_osearch:user", "🔖 <b>Search Orders by Username</b>\n\nSend the username (with or without @).\n\nExample: <code>@durov</code>"),
            "admin_osearch_date": ("admin_osearch:date", "📅 <b>Search Orders by Date</b>\n\nSend a single date or a date range:\n\n<code>2026-09-10</code>\n<code>2026-09-01 2026-09-10</code>"),
        }
        state_value, prompt = prompts[data]
        user_state[uid] = state_value
        await edit_or_send(q, prompt, _osearch_prompt_kb())
        return True

    if data.startswith("admin_osearch_page_"):
        try:
            page = int(data.rsplit("_", 1)[1])
        except Exception:
            page = 0
        ctx = admin_temp_data.get(uid, {}).get("osearch")
        if not ctx:
            await edit_or_send(q, "⌛ <b>That search expired.</b>\n\nPlease run it again.", admin_order_search_menu())
            return True
        await _show_order_search_results(q, uid, ctx, page)
        return True

    # ---------- User card + sub-views ----------
    if data.startswith("admin_usrcard_"):
        try:
            target = int(data.rsplit("_", 1)[1])
        except Exception:
            await edit_or_send(q, "❌ <b>Invalid user.</b>", admin_menu())
            return True
        await _show_admin_user_card(q, target)
        return True

    if data.startswith("admin_usr_orders_"):
        try:
            _parts = data.split("_")
            target, page = int(_parts[3]), int(_parts[4])
        except Exception:
            await edit_or_send(q, "❌ <b>Invalid request.</b>", admin_menu())
            return True
        await _show_admin_user_orders(q, target, page)
        return True

    if data.startswith("admin_usr_deps_"):
        try:
            _parts = data.split("_")
            target, page = int(_parts[3]), int(_parts[4])
        except Exception:
            await edit_or_send(q, "❌ <b>Invalid request.</b>", admin_menu())
            return True
        await _show_admin_user_deposits(q, target, page)
        return True

    if data.startswith("admin_usr_stats_"):
        try:
            target = int(data.rsplit("_", 1)[1])
        except Exception:
            await edit_or_send(q, "❌ <b>Invalid request.</b>", admin_menu())
            return True
        await _show_admin_user_stats(q, target)
        return True

    # ---------- Categories management ----------
    if data == "admin_categories":
        await _show_categories_admin(q)
        return True

    if data == "admin_cat_add":
        user_state[uid] = "admin_cat_create_name"
        admin_temp_data[uid] = {}
        await edit_or_send(
            q,
            """📂 <b>Create New Category</b>

Please enter the category name:""",
            _cat_cancel_kb(),
        )
        return True

    if data == "admin_cat_edit":
        await edit_or_send(q, "📂 <b>Select Category</b>", _category_picker_kb("edit"))
        return True

    if data.startswith("admin_cat_pick:"):
        action = data.split(":", 1)[1]
        prompts = {
            "addex": "📦 <b>Add Existing Product</b>\n\nSelect the target category:",
            "addnew": "➕ <b>Add New Product</b>\n\nSelect the category for the new product:",
            "del": "🗑️ <b>Delete Category</b>\n\nSelect the category to delete:",
        }
        await edit_or_send(q, prompts.get(action, "📂 <b>Select Category</b>"), _category_picker_kb(action))
        return True

    if data.startswith("admin_cat_pickdo:"):
        try:
            _act, _cid = data.split(":")[1], int(data.split(":")[2])
        except Exception:
            await edit_or_send(q, "❌ <b>Invalid request.</b>", _cat_back_kb())
            return True
        if _act == "edit":
            await _show_category_detail(q, _cid)
        elif _act == "del":
            await _admin_cat_delete_confirm(q, _cid)
        elif _act == "addex":
            await _admin_cat_prompt_add_existing(q, uid, _cid)
        elif _act == "addnew":
            await _admin_cat_start_addnew(q, uid, _cid)
        return True

    if data.startswith("admin_cat_sel:"):
        try:
            await _show_category_detail(q, int(data.split(":", 1)[1]))
        except Exception:
            await edit_or_send(q, "❌ <b>Invalid category.</b>", _cat_back_kb())
        return True

    if data.startswith("admin_cat_rename:"):
        try:
            cid = int(data.split(":", 1)[1])
        except Exception:
            await edit_or_send(q, "❌ <b>Invalid category.</b>", _cat_back_kb())
            return True
        cat = get_category(cid)
        if not cat:
            await edit_or_send(q, "❌ <b>Category not found.</b>", _cat_back_kb())
            return True
        user_state[uid] = "admin_cat_rename"
        admin_temp_data.setdefault(uid, {})["cat_rename_id"] = cid
        await edit_or_send(
            q,
            f"""✏️ <b>Rename Category</b>

Current name:
{_cat_name_html(cat[1])}

Send the new category name:""",
            _cat_cancel_kb(),
        )
        return True

    if data.startswith("admin_cat_prods:"):
        try:
            await _show_category_products_admin(q, int(data.split(":", 1)[1]))
        except Exception:
            await edit_or_send(q, "❌ <b>Invalid category.</b>", _cat_back_kb())
        return True

    if data.startswith("admin_cat_addex:"):
        try:
            await _admin_cat_prompt_add_existing(q, uid, int(data.split(":", 1)[1]))
        except Exception:
            await edit_or_send(q, "❌ <b>Invalid category.</b>", _cat_back_kb())
        return True

    if data.startswith("admin_cat_addnew:"):
        try:
            await _admin_cat_start_addnew(q, uid, int(data.split(":", 1)[1]))
        except Exception:
            await edit_or_send(q, "❌ <b>Invalid category.</b>", _cat_back_kb())
        return True

    if data.startswith("admin_cat_rm:"):
        try:
            cid = int(data.split(":", 1)[1])
        except Exception:
            await edit_or_send(q, "❌ <b>Invalid category.</b>", _cat_back_kb())
            return True
        cat = get_category(cid)
        if not cat:
            await edit_or_send(q, "❌ <b>Category not found.</b>", _cat_back_kb())
            return True
        cur.execute("SELECT id, name FROM products WHERE category_id=? ORDER BY id DESC LIMIT 30", (cid,))
        prows = cur.fetchall()
        if not prows:
            await edit_or_send(
                q,
                f"📂 <b>{_cat_name_html(cat[1])}</b>\n\nNo products to remove.",
                _cat_back_kb(f"admin_cat_prods:{cid}"),
            )
            return True
        rows = [[InlineKeyboardButton(f"🆔 {r[0]} — {strip_premium(r[1])[:35]}", callback_data=f"admin_cat_rmok:{cid}:{r[0]}")] for r in prows]
        rows.append([InlineKeyboardButton("◀️ Back", callback_data=f"admin_cat_prods:{cid}")])
        await edit_or_send(
            q,
            f"""➖ <b>Remove Product</b>

Tap a product to remove it from 📂 <b>{_cat_name_html(cat[1])}</b>.

<i>The product itself is NOT deleted -- it becomes Uncategorized.</i>""",
            InlineKeyboardMarkup(rows),
        )
        return True

    if data.startswith("admin_cat_rmok:"):
        try:
            _cid, _pid = int(data.split(":")[1]), int(data.split(":")[2])
        except Exception:
            await edit_or_send(q, "❌ <b>Invalid request.</b>", _cat_back_kb())
            return True
        prow = _find_product_for_category(_pid)
        assign_product_category(_pid, None)
        notice = f"✅ Removed: {_cat_name_html(prow[1])} → Uncategorized" if prow else None
        await _show_category_products_admin(q, _cid, notice=notice)
        return True

    if data.startswith("admin_cat_del:"):
        try:
            await _admin_cat_delete_confirm(q, int(data.split(":", 1)[1]))
        except Exception:
            await edit_or_send(q, "❌ <b>Invalid category.</b>", _cat_back_kb())
        return True

    if data.startswith("admin_cat_delok:"):
        try:
            cid = int(data.split(":", 1)[1])
        except Exception:
            await edit_or_send(q, "❌ <b>Invalid category.</b>", _cat_back_kb())
            return True
        cat = get_category(cid)
        if not cat:
            await edit_or_send(q, "❌ <b>Category not found.</b>", _cat_back_kb())
            return True
        count = _cat_products_count(cid)
        cname = strip_premium(cat[1])
        delete_category(cid)  # products -> Uncategorized (NULL), never deleted
        log_admin_action(uid, "DELETE_CATEGORY", f"Category {cname} | {count} products uncategorized")
        msg = "✅ <b>Category deleted.</b>\n\n"
        msg += f"The {count} products have been moved to Uncategorized." if count else "It had no products."
        await edit_or_send(q, msg, _cat_back_kb())
        return True

    if data == "admin_cat_done":
        datax = admin_temp_data.pop(uid, {})
        user_state.pop(uid, None)
        cid = datax.get("cat_create_id")
        cat = get_category(cid) if cid else None
        added = datax.get("cat_added", [])
        if not cat:
            await edit_or_send(q, "❌ <b>Session expired.</b>", _cat_back_kb())
            return True
        await edit_or_send(
            q,
            f"""✅ <b>Category created.</b>

📂 <b>{_cat_name_html(cat[1])}</b>
📦 <b>Products:</b> <code>{len(added)}</code>""",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 Manage this category", callback_data=f"admin_cat_prods:{cat[0]}")],
                [InlineKeyboardButton("◀️ Back", callback_data="admin_categories")],
            ]),
        )
        return True

    if data == "admin_cat_cancel":
        datax = admin_temp_data.pop(uid, {})
        user_state.pop(uid, None)
        cid = datax.get("cat_create_id")
        # Aborting creation before any product was added removes the empty shell.
        if cid and get_category(cid) and _cat_products_count(cid) == 0:
            delete_category(cid)
        await edit_or_send(q, "❌ <b>Cancelled.</b>", admin_categories_menu())
        return True

    if data == "admin_cat_moveall":
        mv = admin_temp_data.get(uid, {}).get("cat_move") or {}
        pids = mv.get("pids") or []
        target = get_category(mv.get("target")) if mv.get("target") else None
        if not pids or not target:
            await edit_or_send(q, "❌ <b>Request expired.</b> Please try again.", _cat_back_kb())
            return True
        moved = 0
        last_name = None
        for pid in pids:
            prow = _find_product_for_category(pid)
            if not prow:
                continue
            assign_product_category(pid, target[0])
            moved += 1
            last_name = prow[1]
            if mv.get("in_create"):
                admin_temp_data.setdefault(uid, {}).setdefault("cat_added", []).append((int(pid), prow[1]))
        admin_temp_data.get(uid, {}).pop("cat_move", None)
        if moved == 0:
            await edit_or_send(q, "❌ <b>Request expired.</b> Please try again.", _cat_back_kb())
            return True
        if moved == 1:
            text = f"✅ <b>Product moved successfully.</b>\n\n{_cat_name_html(last_name)}\n⬇️\n📂 {_cat_name_html(target[1])}"
        else:
            text = f"✅ <b>{moved} products moved successfully.</b>\n\n⬇️\n📂 {_cat_name_html(target[1])}"
        await edit_or_send(
            q,
            text,
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 Manage this category", callback_data=f"admin_cat_prods:{target[0]}")],
                [InlineKeyboardButton("◀️ Back", callback_data="admin_categories")],
            ]),
        )
        return True

    if data.startswith("admin_cat_move:"):
        # Legacy single-product confirm (pre-batch buttons on old messages).
        await edit_or_send(q, "❌ <b>Request expired.</b> Please try again.", _cat_back_kb())
        return True

    if data == "admin_cat_movecancel":
        admin_temp_data.get(uid, {}).pop("cat_move", None)
        await edit_or_send(q, "❌ <b>Move cancelled.</b>", _cat_back_kb())
        return True

    return False


async def handle_admin_message(update, context, uid, text, state):
    if state and state.startswith("admin_m365_sess_label:") and is_admin(uid):
        try:
            sess_id = int(state.split(":")[1])
        except Exception:
            sess_id = 0
        user_state.pop(uid, None)
        new_label = text.strip()[:60]
        if sess_id and new_label and new_label != "-":
            rename_m365_session(sess_id, new_label)
            await update.message.reply_text(f"✅ <b>Session #{sess_id} named:</b> {html.escape(new_label, quote=False)}", parse_mode="HTML")
        else:
            await update.message.reply_text("👍 Keeping the automatic name.", parse_mode="HTML")
        return True

    if state == "admin_m365_sess_limit" and is_admin(uid):
        raw = text.strip()
        if not raw.isdigit() or int(raw) <= 0 or int(raw) > 50:
            await update.message.reply_text("❌ Send a whole number between 1 and 50.\n\nExample: <code>5</code>", parse_mode="HTML")
            return True
        set_setting("m365_invites_per_session", raw)
        user_state.pop(uid, None)
        await update.message.reply_text(f"✅ <b>Each Microsoft ID will now send {raw} invites before rotating to the next one.</b>", parse_mode="HTML")
        return True

    if state == "admin_warr_hours" and is_admin(uid):
        try:
            w_hours = int(float(text.strip()))
            if w_hours <= 0 or w_hours > 720:
                raise ValueError("range")
        except Exception:
            await update.message.reply_text("❌ Send a number of hours between 1 and 720.", parse_mode="HTML")
            return True
        set_setting("warranty_hours", str(w_hours))
        user_state.pop(uid, None)
        await update.message.reply_text(f"✅ <b>Warranty window set to {w_hours} hours.</b>", parse_mode="HTML")
        return True

    if state == "admin_warr_banuser" and is_admin(uid):
        try:
            ban_target = int(text.strip())
        except Exception:
            await update.message.reply_text("❌ Send a numeric user ID.", parse_mode="HTML")
            return True
        user_state.pop(uid, None)
        if is_warranty_banned(ban_target):
            set_warranty_ban(ban_target, False)
            await update.message.reply_text(f"🟢 <b>User <code>{ban_target}</code> warranty-unbanned.</b> Auto-replacement works for them again.", parse_mode="HTML")
        else:
            set_warranty_ban(ban_target, True)
            await update.message.reply_text(f"🚫 <b>User <code>{ban_target}</code> warranty-banned.</b> Their claims now always need your approval.", parse_mode="HTML")
        return True

    if state == "admin_gift_amount" and is_admin(uid):
        try:
            g_amount = round(float(text.strip()), 2)
            if g_amount <= 0:
                raise ValueError("range")
        except Exception:
            await update.message.reply_text("❌ Send a valid amount, example: <code>10</code>", parse_mode="HTML")
            return True
        gift_code = create_gift_code(uid, g_amount, free=True)
        user_state.pop(uid, None)
        await update.message.reply_text(
            f"✅ <b>Gift Code Created</b>\n\n🎁 <code>{gift_code}</code>\n💰 <code>{g_amount:.2f} USDT</code>\n\nShare it — anyone can redeem it once for wallet balance.",
            parse_mode="HTML"
        )
        return True


    if state == "admin_m365_test_email" and is_admin(uid):
        email = text.strip()
        if "@" not in email or "." not in email:
            await update.message.reply_text("❌ Send a valid email address.", parse_mode="HTML")
            return True

        user_state.pop(uid, None)
        await update.message.reply_text(
            "🧪 <b>Sending test invite...</b>\n\nThis can take up to a minute, please wait.",
            parse_mode="HTML"
        )

        try:
            from services.m365_family import send_family_invite
            ok, result_msg, screenshot = await send_family_invite(email)
        except Exception as exc:
            ok, result_msg, screenshot = False, str(exc), None

        icon = "✅" if ok else "❌"
        await update.message.reply_text(
            f"{icon} <b>Test result:</b> {html.escape(str(result_msg)[:800], quote=False)}",
            reply_markup=admin_menu(),
            parse_mode="HTML"
        )

        try:
            from services.m365_family import pop_rotation_note
            rotation_note = pop_rotation_note()
        except Exception:
            rotation_note = None
        if rotation_note:
            try:
                await update.message.reply_text("🔄 <b>M365 Session Rotation</b>\n\n" + rotation_note, parse_mode="HTML")
            except Exception:
                pass

        if screenshot:
            try:
                import io as _io
                await update.message.reply_photo(photo=_io.BytesIO(screenshot), caption="📸 Microsoft page screenshot")
            except Exception:
                pass
        return True

    if state == "admin_canva_test_email" and is_admin(uid):
        email = text.strip()
        if "@" not in email or "." not in email:
            await update.message.reply_text("❌ Send a valid email address.", parse_mode="HTML")
            return True

        user_state.pop(uid, None)
        await update.message.reply_text(
            "🧪 <b>Sending test Canva invite...</b>\n\nThis can take up to a minute, please wait.",
            parse_mode="HTML"
        )

        try:
            from services.canva_invite import send_canva_invite
            ok, result_msg, screenshot = await send_canva_invite(email)
        except Exception as exc:
            ok, result_msg, screenshot = False, str(exc), None

        icon = "✅" if ok else "❌"
        await update.message.reply_text(
            f"{icon} <b>Test result:</b> {html.escape(str(result_msg)[:800], quote=False)}",
            reply_markup=admin_menu(),
            parse_mode="HTML"
        )

        if screenshot:
            try:
                import io as _io
                await update.message.reply_photo(photo=_io.BytesIO(screenshot), caption="📸 Canva confirmation screenshot")
            except Exception:
                pass
        return True

    if state == "admin_set_welcome_coupon" and is_admin(uid):
        value = "" if text.strip().upper() == "OFF" else text.strip().upper()
        set_setting("auto_welcome_coupon", value)
        user_state.pop(uid, None)

        await update.message.reply_text(
            f"✅ <b>Welcome Coupon Updated</b>\n\n🎁 Coupon: <code>{value or 'OFF'}</code>",
            reply_markup=auto_marketing_keyboard(),
            parse_mode="HTML"
        )
        return True

    if state == "admin_set_inactive_coupon" and is_admin(uid):
        value = "" if text.strip().upper() == "OFF" else text.strip().upper()
        set_setting("inactive_coupon", value)
        user_state.pop(uid, None)

        await update.message.reply_text(
            f"✅ <b>Inactive Coupon Updated</b>\n\n😴 Coupon: <code>{value or 'OFF'}</code>",
            reply_markup=auto_marketing_keyboard(),
            parse_mode="HTML"
        )
        return True

    if state == "admin_set_inactive_days" and is_admin(uid):
        try:
            days = int(text.strip())
            set_setting("inactive_days", str(days))
            user_state.pop(uid, None)

            await update.message.reply_text(
                f"✅ <b>Inactive Days Updated</b>\n\n📅 Days: <code>{days}</code>",
                reply_markup=auto_marketing_keyboard(),
                parse_mode="HTML"
            )
        except Exception:
            await update.message.reply_text(
                "❌ Send a number only.\n\nExample: <code>7</code>",
                parse_mode="HTML"
            )
        return True

    if state == "admin_schedule_broadcast" and is_admin(uid):
        try:
            text = premium_html(update, text)
            run_at, message = [x.strip() for x in text.split("|", 1)]
            # Validate rough format
            datetime.strptime(run_at, "%Y-%m-%d %H:%M")
            bid = create_scheduled_broadcast(message, run_at + ":00")
            user_state.pop(uid, None)
            await update.message.reply_text(
                f"✅ <b>Broadcast Scheduled</b>\n\n🧾 ID: <code>#{bid}</code>\n⏰ Time: <code>{run_at}</code>\n\n{message}",
                reply_markup=scheduled_broadcast_keyboard(),
                parse_mode="HTML"
            )
        except Exception:
            await update.message.reply_text(
                "❌ <b>Wrong format.</b>\n\nUse:\n<code>2026-06-01 20:00 | Your message</code>",
                parse_mode="HTML"
            )
            return True
        return True

    if state == "admin_set_first_discount" and is_admin(uid):
        try:
            value = float(text.strip())
            set_setting("first_purchase_discount_percent", str(value))
            user_state.pop(uid, None)
            await update.message.reply_text("✅ First purchase discount updated.", reply_markup=admin_sales_settings_keyboard(), parse_mode="HTML")
        except Exception:
            await update.message.reply_text("❌ Send a number like <code>10</code>.", parse_mode="HTML")
        return True

    if state == "admin_set_loyalty_percent" and is_admin(uid):
        try:
            value = float(text.strip())
            set_setting("loyalty_percent", str(value))
            user_state.pop(uid, None)
            await update.message.reply_text("✅ Loyalty percent updated.", reply_markup=admin_sales_settings_keyboard(), parse_mode="HTML")
        except Exception:
            await update.message.reply_text("❌ Send a number like <code>2</code>.", parse_mode="HTML")
        return True

    if state == "admin_set_buyxgety" and is_admin(uid):
        try:
            x, y = [int(v.strip()) for v in text.split("|", 1)]
            set_setting("buy_x_qty", str(x))
            set_setting("buy_x_free", str(y))
            user_state.pop(uid, None)
            await update.message.reply_text("✅ Buy X Get Y updated.", reply_markup=admin_sales_settings_keyboard(), parse_mode="HTML")
        except Exception:
            await update.message.reply_text("❌ Use format: <code>5 | 1</code>", parse_mode="HTML")
        return True

    if state == "admin_bundle_create" and is_admin(uid):
        try:
            parts = [x.strip() for x in text.split("|")]
            if len(parts) != 3:
                raise ValueError("format")

            bundle_name = parts[0]
            bundle_price = float(parts[1])
            if not bundle_name or bundle_price <= 0:
                raise ValueError("price")

            bundle_items = []
            for chunk in parts[2].split(","):
                pid_s, qty_s = chunk.strip().split(":")
                item_pid = int(pid_s)
                item_qty = int(qty_s)
                if item_qty <= 0:
                    raise ValueError("qty")
                cur.execute("SELECT id FROM products WHERE id=? AND active=1", (item_pid,))
                if not cur.fetchone():
                    await update.message.reply_text(
                        f"❌ Product ID <b>{item_pid}</b> not found or inactive.",
                        parse_mode="HTML"
                    )
                    return True
                bundle_items.append((item_pid, item_qty))

            if not bundle_items:
                raise ValueError("empty")

            new_bundle_id = create_bundle(bundle_name, bundle_price, bundle_items)
            user_state.pop(uid, None)
            await update.message.reply_text(
                f"✅ Bundle <b>#{new_bundle_id}</b> created (${bundle_price:.2f}).\n\n"
                "It now appears in the shop under 🎁 Bundle Deals.",
                parse_mode="HTML"
            )
        except ValueError:
            await update.message.reply_text(
                "❌ Use format: <code>Bundle Name | 9.99 | 12:1, 15:2</code>",
                parse_mode="HTML"
            )
        return True

    if state == "admin_ban_user" and is_admin(uid):
        try:
            target = int(text.strip())
            block_user(target)
            user_state.pop(uid, None)
            await update.message.reply_text(f"🚫 User banned: <code>{target}</code>", reply_markup=user_management_keyboard(), parse_mode="HTML")
        except Exception:
            await update.message.reply_text("❌ Send valid user ID.", parse_mode="HTML")
        return True

    if state == "admin_unban_user" and is_admin(uid):
        try:
            target = int(text.strip())
            unblock_user(target)
            user_state.pop(uid, None)
            await update.message.reply_text(f"✅ User unbanned: <code>{target}</code>", reply_markup=user_management_keyboard(), parse_mode="HTML")
        except Exception:
            await update.message.reply_text("❌ Send valid user ID.", parse_mode="HTML")
        return True

    if state == "admin_reseller_coupon_create" and is_admin(uid):
        try:
            parts = [x.strip() for x in text.split("|")]
            if len(parts) < 5:
                raise ValueError("Missing fields")

            code = parts[0].upper()
            dtype = parts[1].lower()
            value = float(parts[2])
            max_uses = int(parts[3]) if parts[3] else 0
            hours = float(parts[4]) if parts[4] else 24

            if dtype not in ("percent", "fixed"):
                raise ValueError("discount type must be percent or fixed")

            if hours <= 0:
                hours = 24

            ends = (now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

            cur.execute(
                """
                INSERT OR REPLACE INTO coupons(code, discount_type, value, active, max_uses, used_count, expires_at, reseller)
                VALUES(?,?,?,?,?,COALESCE((SELECT used_count FROM coupons WHERE code=?),0),?,1)
                """,
                (code, dtype, value, 1, max_uses, code, ends)
            )
            conn.commit()
            user_state.pop(uid, None)

            discount_text = f"{value:g}%" if dtype == "percent" else f"${value:.2f}"

            await update.message.reply_text(
                f"""🤝 <b>Reseller Coupon Saved</b>

Code: <code>{code}</code>
💝 Discount: <code>{discount_text}</code>
⏳ Validity: <code>{hours:g} hours</code>
📌 Expires: <code>{ends}</code>

🔒 <b>Private:</b> this coupon was <b>NOT</b> broadcast. Share the code only with your reseller.""",
                reply_markup=admin_coupons_keyboard(),
                parse_mode="HTML"
            )

        except Exception:
            await update.message.reply_text(
                "❌ <b>Wrong Format</b>\n\nUse:\n<code>RESELLER10 | percent | 10 | 100 | 720</code>",
                parse_mode="HTML"
            )
            return True
        return True

    if state == "admin_coupon_create" and is_admin(uid):
        try:
            parts = [x.strip() for x in text.split("|")]
            if len(parts) < 5:
                raise ValueError("Missing fields")

            code = parts[0].upper()
            dtype = parts[1].lower()
            value = float(parts[2])
            max_uses = int(parts[3]) if parts[3] else 0
            hours = float(parts[4]) if parts[4] else 24

            if dtype not in ("percent", "fixed"):
                raise ValueError("discount type must be percent or fixed")

            if hours <= 0:
                hours = 24

            ends = (now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

            cur.execute(
                """
                INSERT OR REPLACE INTO coupons(code, discount_type, value, active, max_uses, used_count, expires_at)
                VALUES(?,?,?,?,?,COALESCE((SELECT used_count FROM coupons WHERE code=?),0),?)
                """,
                (code, dtype, value, 1, max_uses, code, ends)
            )
            conn.commit()
            user_state.pop(uid, None)

            sent, failed = await broadcast_coupon_created(context, code, dtype, value, max_uses, ends)

            discount_text = f"{value:g}%" if dtype == "percent" else f"${value:.2f}"

            await update.message.reply_text(
                f"""✅ <b>Coupon Saved</b>

🎟 Code: <code>{code}</code>
💝 Discount: <code>{discount_text}</code>
⏳ Validity: <code>{hours:g} hours</code>
📌 Expires: <code>{ends}</code>
📢 Broadcast sent to <code>{sent}</code> users""",
                reply_markup=admin_coupons_keyboard(),
                parse_mode="HTML"
            )

        except Exception:
            await update.message.reply_text(
                "❌ <b>Wrong Format</b>\n\nUse:\n<code>WELCOME10 | percent | 10 | 100 | 24</code>",
                parse_mode="HTML"
            )
            return True
        return True

    if state == "admin_coupon_delete" and is_admin(uid):
        code = text.strip().upper()
        cur.execute("DELETE FROM coupons WHERE code=?", (code,))
        cur.execute("DELETE FROM user_coupons WHERE code=?", (code,))
        conn.commit()
        user_state.pop(uid, None)
        await update.message.reply_text(
            f"🗑 <b>Coupon deleted:</b> <code>{code}</code>",
            reply_markup=admin_coupons_keyboard(),
            parse_mode="HTML"
        )
        return True

    if state and state.startswith("admin_flash_set:") and is_admin(uid):
        try:
            product_id = int(state.split(":")[1])
            price_text, hours_text = [x.strip() for x in text.split("|", 1)]
            flash_price = float(price_text)
            hours = _parse_hours(hours_text)
            ends = (now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

            if flash_price <= 0:
                raise ValueError("Flash price must be more than 0")

            cur.execute("SELECT name, price FROM products WHERE id=?", (product_id,))
            product_row = cur.fetchone()
            product_name = product_row[0] if product_row else f"Product #{product_id}"
            old_price = float(product_row[1]) if product_row else flash_price

            cur.execute("UPDATE products SET flash_price=?, flash_ends_at=? WHERE id=?", (flash_price, ends, product_id))
            conn.commit()
            user_state.pop(uid, None)
            log_admin_action(uid, "FLASH_SALE_SET", f"Product #{product_id} | {flash_price} | ends {ends}")

            sent, failed = await broadcast_flash_sale(
                context,
                product_id,
                product_name,
                old_price,
                flash_price,
                ends,
            )

            await update.message.reply_text(
                f"""✅ <b>Flash Sale Set</b>

🆔 Product: <code>{product_id}</code>
📦 Name: <b>{_admin_name(product_name)}</b>
💵 Old Price: <code>${old_price:.2f}</code>
🔥 Sale Price: <code>${flash_price:.2f}</code>
⏳ Ends: <code>{ends}</code>

📢 Broadcast sent to <code>{sent}</code> users""",
                reply_markup=flash_sale_keyboard(product_id),
                parse_mode="HTML"
            )

        except Exception:
            await update.message.reply_text(
                "❌ <b>Wrong Format</b>\n\nUse:\n<code>0.70 | 24</code>",
                parse_mode="HTML"
            )
            return True
        return True

    # ---------- Order search modes (results are paginated via _show_order_search_results) ----------
    if state == "admin_osearch:id" and is_admin(uid):
        raw = text.strip().lstrip("#")
        if not raw.isdigit():
            await update.message.reply_text(
                "❌ <b>Invalid Order ID.</b>\n\nSend digits only. Example: <code>123</code>",
                parse_mode="HTML",
            )
            return True
        order_id = int(raw)
        user_state.pop(uid, None)
        detail_text, target_user, _delivery = get_order_detail_text(order_id)
        if not detail_text:
            await update.message.reply_text(
                f"❌ <b>Order <code>#{order_id}</code> not found.</b>",
                reply_markup=admin_order_search_menu(),
                parse_mode="HTML",
            )
            return True
        await update.message.reply_text(
            detail_text,
            reply_markup=admin_order_detail_keyboard(order_id, target_user),
            parse_mode="HTML",
        )
        return True

    if state == "admin_osearch:uid" and is_admin(uid):
        raw = text.strip()
        if not raw.isdigit():
            await update.message.reply_text(
                "❌ <b>Invalid User ID.</b>\n\nSend digits only. Example: <code>7499348138</code>",
                parse_mode="HTML",
            )
            return True
        target = int(raw)
        user_state.pop(uid, None)
        ctx = ("uid", target)
        admin_temp_data.setdefault(uid, {})["osearch"] = ctx
        await _show_order_search_results(_MsgQuery(update.message, update.message.from_user), uid, ctx, 0)
        return True

    if state == "admin_osearch:user" and is_admin(uid):
        uname = text.strip().lstrip("@")
        if not uname:
            await update.message.reply_text(
                "❌ Send a username, e.g. <code>@durov</code> or <code>durov</code>.",
                parse_mode="HTML",
            )
            return True
        user_state.pop(uid, None)
        ctx = ("user", uname)
        admin_temp_data.setdefault(uid, {})["osearch"] = ctx
        await _show_order_search_results(_MsgQuery(update.message, update.message.from_user), uid, ctx, 0)
        return True

    if state == "admin_osearch:date" and is_admin(uid):
        parts = text.split()
        if len(parts) == 1:
            date_from = date_to = parts[0]
        elif len(parts) == 2:
            date_from, date_to = parts
        else:
            date_from = date_to = None
        try:
            if not date_from:
                raise ValueError("bad range")
            count_search_orders(date_from=date_from, date_to=date_to)  # validates format
        except ValueError:
            await update.message.reply_text(
                "❌ <b>Invalid date.</b>\n\nUse <code>YYYY-MM-DD</code> or a range: <code>2026-09-01 2026-09-10</code>",
                parse_mode="HTML",
            )
            return True
        user_state.pop(uid, None)
        ctx = ("date", (date_from, date_to))
        admin_temp_data.setdefault(uid, {})["osearch"] = ctx
        await _show_order_search_results(_MsgQuery(update.message, update.message.from_user), uid, ctx, 0)
        return True

    if state and state.startswith("admin_order_refund_confirm:") and is_admin(uid):
        try:
            order_id = int(state.split(":")[1])
            if text.strip().upper() != "REFUND":
                raise ValueError("Missing REFUND")
            cur.execute("SELECT user_id, product_name, price FROM orders WHERE id=?", (order_id,))
            order = cur.fetchone()
            if not order:
                await update.message.reply_text("❌ <b>Order not found.</b>", parse_mode="HTML")
                user_state.pop(uid, None)
                return True
            target_id, product_name, price = order
            cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (price, target_id))
            conn.commit()
            log_admin_action(uid, "ORDER_DETAIL_REFUND", f"Order #{order_id} | user {target_id} | amount {price:.2f}")
            user_state.pop(uid, None)
            await update.message.reply_text(f"""✅ <b>Order Refunded</b>

🧾 <b>Order:</b> <code>#{order_id}</code>
👤 <b>User:</b> <code>{target_id}</code>
📦 <b>Product:</b> {_admin_name(product_name)}
💰 <b>Refunded:</b> <code>${price:.2f}</code>""", reply_markup=admin_order_detail_keyboard(order_id, target_id), parse_mode="HTML")
            try:
                await context.bot.send_message(chat_id=target_id, text=f"💸 <b>Your order was refunded</b>\n\n🧾 <b>Order:</b> <code>#{order_id}</code>\n📦 <b>Product:</b> {_admin_name(product_name)}\n💰 <b>Refund:</b> <code>${price:.2f}</code>", parse_mode="HTML")
            except Exception:
                pass
        except Exception:
            await update.message.reply_text("❌ Send <code>REFUND</code> to confirm.", parse_mode="HTML")
            return True
        return True

    if state and state.startswith("admin_order_delete_confirm:") and is_admin(uid):
        try:
            order_id = int(state.split(":")[1])
            if text.strip().upper() != "DELETE":
                raise ValueError("Missing DELETE")
            cur.execute("DELETE FROM orders WHERE id=?", (order_id,))
            conn.commit()
            log_admin_action(uid, "ORDER_LOG_DELETE", f"Order #{order_id}")
            user_state.pop(uid, None)
            await update.message.reply_text(f"✅ <b>Order log deleted.</b>\n\n🧾 Order: <code>#{order_id}</code>", reply_markup=admin_orders_list_keyboard(), parse_mode="HTML")
        except Exception:
            await update.message.reply_text("❌ Send <code>DELETE</code> to confirm.", parse_mode="HTML")
            return True
        return True

    if state and state.startswith("admin_offer_add:") and is_admin(uid):
        try:
            product_id = int(state.split(":")[1])
            lines = [x.strip() for x in text.splitlines() if x.strip()]
            if not lines:
                raise ValueError("No offers found")

            saved = []
            for line in lines:
                if "|" not in line:
                    raise ValueError("Use QUANTITY | PRICE format")
                qty_text, price_text = [x.strip() for x in line.split("|", 1)]
                qty = int(qty_text)
                offer_price = float(price_text)
                if qty <= 0 or offer_price <= 0:
                    raise ValueError("Quantity and price must be greater than 0")
                set_product_price_tier(product_id, qty, offer_price)
                saved.append((qty, offer_price))

            log_admin_action(uid, "PRICE_OFFER_SET", f"Product {product_id} | {saved}")
            user_state.pop(uid, None)

            sent, failed = await broadcast_new_offer(context, product_id)

            await update.message.reply_text(
                f"✅ <b>Price offers saved.</b>\n📢 <b>Offer alert broadcast:</b> sent to <code>{sent}</code> users (failed: <code>{failed}</code>).",
                reply_markup=price_offers_keyboard(product_id),
                parse_mode="HTML"
            )
            await update.message.reply_text(
                get_price_offers_text(product_id),
                reply_markup=price_offers_keyboard(product_id),
                parse_mode="HTML"
            )

        except Exception as exc:
            await update.message.reply_text(
                f"❌ <b>Offer save failed.</b>\n\n<code>{exc}</code>",
                parse_mode="HTML"
            )
            return True
        return True

    if state and state.startswith("admin_offer_delete:") and is_admin(uid):
        try:
            product_id = int(state.split(":")[1])
            qty = int(text.strip())
            deleted = delete_product_price_tier(product_id, qty)
            user_state.pop(uid, None)

            await update.message.reply_text(
                f"{'✅' if deleted else '⚠️'} <b>Offer delete completed.</b>\n\nQuantity: <code>{qty}</code>",
                reply_markup=price_offers_keyboard(product_id),
                parse_mode="HTML"
            )

        except Exception:
            await update.message.reply_text("❌ Send only quantity. Example: <code>5</code>", parse_mode="HTML")
            return True
        return True

    if state and state.startswith("admin_offer_clear:") and is_admin(uid):
        try:
            product_id = int(state.split(":")[1])
            if text.strip().upper() != "CLEAR":
                raise ValueError("Missing CLEAR")
            clear_product_price_tiers(product_id)
            user_state.pop(uid, None)
            log_admin_action(uid, "PRICE_OFFERS_CLEAR", f"Product {product_id}")

            await update.message.reply_text(
                "✅ <b>All price offers cleared.</b>",
                reply_markup=price_offers_keyboard(product_id),
                parse_mode="HTML"
            )

        except Exception:
            await update.message.reply_text("❌ Send <code>CLEAR</code> to confirm.", parse_mode="HTML")
            return True
        return True

    if state == "admin_add_product_name" and is_admin(uid):
        # Preserve Telegram premium emojis in the name as <tg-emoji> HTML tags.
        admin_temp_data.setdefault(uid, {})["name"] = sanitize_premium_name(premium_html(update, text).strip())
        user_state[uid] = "admin_add_product_price"
        await update.message.reply_text(
            "💰 <b>Step 2/7</b>\n\nSend product price.\n\nExample:\n<code>0.70</code>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_add_cancel")]]),
            parse_mode="HTML"
        )
        return True

    if state == "admin_add_product_price" and is_admin(uid):
        try:
            admin_temp_data.setdefault(uid, {})["price"] = float(text)
            user_state[uid] = "admin_add_product_description"
            await update.message.reply_text(
                "📝 <b>Step 3/7</b>\n\nSend product description or skip.",
                reply_markup=add_product_skip_keyboard(),
                parse_mode="HTML"
            )
        except Exception:
            await update.message.reply_text("❌ Send only price, example: <code>0.70</code>", parse_mode="HTML")
        return True

    if state == "admin_add_product_description" and is_admin(uid):
        admin_temp_data.setdefault(uid, {})["description"] = premium_html(update, text)
        user_state[uid] = "admin_add_product_validity"
        await update.message.reply_text(
            "📌 <b>Step 4/7</b>\n\nSend validity or skip.\n\nExample:\n<code>30 Days</code>",
            reply_markup=add_product_skip_keyboard(),
            parse_mode="HTML"
        )
        return True

    if state == "admin_add_product_validity" and is_admin(uid):
        admin_temp_data.setdefault(uid, {})["validity"] = premium_html(update, text)
        user_state[uid] = "admin_add_product_warranty"
        await update.message.reply_text(
            "🛡 <b>Step 5/7</b>\n\nSend warranty or skip.\n\nExample:\n<code>Replacement if invalid</code>",
            reply_markup=add_product_skip_keyboard(),
            parse_mode="HTML"
        )
        return True

    if state == "admin_add_product_warranty" and is_admin(uid):
        admin_temp_data.setdefault(uid, {})["warranty"] = premium_html(update, text)
        user_state[uid] = "admin_add_product_rules"
        await update.message.reply_text(
            "📜 <b>Step 6/7</b>\n\nSend rules/terms or skip.",
            reply_markup=add_product_skip_keyboard(),
            parse_mode="HTML"
        )
        return True

    if state == "admin_add_product_rules" and is_admin(uid):
        admin_temp_data.setdefault(uid, {})["rules"] = premium_html(update, text)
        user_state[uid] = "admin_add_product_photo"
        await update.message.reply_text(
            "🖼 <b>Step 7/7</b>\n\nSend product photo or skip.",
            reply_markup=add_product_photo_keyboard(),
            parse_mode="HTML"
        )
        return True

    if state == "admin_add_product_photo" and is_admin(uid):
        await update.message.reply_text(
            "🖼 Please send a photo/image, or tap Skip Photo.",
            reply_markup=add_product_photo_keyboard(),
            parse_mode="HTML"
        )
        return True

    if state == "admin_add_product_stock" and is_admin(uid):
        if text.strip().lower() in ("skip", "/skip", "-"):
            items = []
        else:
            items = [x.strip() for x in text.replace(";;", "\n").splitlines() if x.strip()]
        admin_temp_data.setdefault(uid, {})["stock_items"] = items
        user_state[uid] = "admin_add_product_preview"
        await update.message.reply_text(
            add_product_preview_text(uid),
            reply_markup=add_product_preview_keyboard(),
            parse_mode="HTML"
        )
        return True

    if state and state.startswith("admin_codes_add:") and is_admin(uid):
        try:
            product_id = int(state.split(":")[1])

            cur.execute("SELECT name, price FROM products WHERE id=?", (product_id,))
            product = cur.fetchone()
            if not product:
                await update.message.reply_text("❌ <b>Product not found.</b>", parse_mode="HTML")
                user_state.pop(uid, None)
                return True

            delivery = text.replace("\n", ";;")
            added = restock_product(product_id, delivery)
            available = get_available_stock_count(product_id)

            cur.execute("UPDATE products SET stock=? WHERE id=?", (available, product_id))
            conn.commit()

            product_name, product_price = product
            log_admin_action(uid, "CODE_MANAGER_ADD", f"Product ID {product_id} | added {added} | available {available}")

            try:
                sent, failed = await broadcast_product_update(
                    context,
                    product_id,
                    product_name,
                    float(product_price),
                    added,
                    available,
                    mode="stock",
                )
            except Exception:
                sent, failed = 0, 0

            user_state.pop(uid, None)

            await update.message.reply_text(
                f"""✅ <b>Codes Added</b>

📦 Product: <b>{_admin_name(product_name)}</b>
➕ Added: <code>{added}</code>
📊 Available Now: <code>{available}</code>
📢 Broadcast: sent to <code>{sent}</code> users""",
                reply_markup=product_codes_keyboard(product_id),
                parse_mode="HTML"
            )

            msg = get_product_codes_text(product_id) or "✅ Codes added."
            await update.message.reply_text(
                msg,
                reply_markup=product_codes_keyboard(product_id),
                parse_mode="HTML"
            )

        except Exception as exc:
            await update.message.reply_text(
                f"❌ <b>Add codes failed.</b>\n\n<code>{exc}</code>",
                parse_mode="HTML"
            )
            return True

        return True

    if state and state.startswith("admin_codes_delete:") and is_admin(uid):
        try:
            product_id = int(state.split(":")[1])
            raw = text.strip()

            if "DELETE" not in raw.upper().split():
                raise ValueError("Confirmation word DELETE missing")

            clean = raw.replace(",", " ").replace("DELETE", "").replace("delete", "")
            stock_ids = [int(x) for x in clean.split() if x.strip().isdigit()]

            if not stock_ids:
                raise ValueError("No Stock IDs found")

            deleted = []
            skipped = []

            for sid in stock_ids:
                cur.execute("SELECT id FROM product_stock WHERE id=? AND product_id=?", (sid, product_id))
                row = cur.fetchone()

                if not row:
                    skipped.append(sid)
                    continue

                cur.execute("DELETE FROM product_stock WHERE id=? AND product_id=?", (sid, product_id))
                deleted.append(sid)

            available = get_available_stock_count(product_id)
            cur.execute("UPDATE products SET stock=? WHERE id=?", (available, product_id))
            conn.commit()

            log_admin_action(uid, "CODE_MANAGER_DELETE", f"Product ID {product_id} | deleted {deleted} | skipped {skipped}")

            user_state.pop(uid, None)

            msg = f"""🗑 <b>Codes Permanently Deleted</b>

🆔 <b>Product ID:</b> <code>{product_id}</code>
✅ <b>Deleted Stock IDs:</b> <code>{', '.join(map(str, deleted)) if deleted else 'None'}</code>
⚠️ <b>Skipped:</b> <code>{', '.join(map(str, skipped)) if skipped else 'None'}</code>
📊 <b>Available Now:</b> <code>{available}</code>"""

            await update.message.reply_text(
                msg,
                reply_markup=product_codes_keyboard(product_id),
                parse_mode="HTML"
            )

        except Exception:
            await update.message.reply_text(
                """❌ <b>Wrong Format</b>

Use:
<code>12,15,18 DELETE</code>

You must include the confirmation word <code>DELETE</code>.""",
                parse_mode="HTML"
            )
            return True

        return True

    if state == "admin_set_welcome" and is_admin(uid):
        value = premium_html(update, text).strip()
        user_state.pop(uid, None)
        if value.lower() == "reset":
            cur.execute("DELETE FROM settings WHERE key=?", ("welcome_message",))
            conn.commit()
            await update.message.reply_text("♻️ <b>Welcome message reset to default.</b>", parse_mode="HTML")
        else:
            set_setting("welcome_message", value)
            log_admin_action(uid, "EDIT_WELCOME", "Welcome message updated")
            await update.message.reply_text("✅ <b>Welcome message updated.</b>\n\nSend /start to preview it.", parse_mode="HTML")
        return True

    if state and state.startswith("admin_set_btn:") and is_admin(uid):
        key = state.split(":", 1)[1]
        user_state.pop(uid, None)
        raw = premium_html(update, text).strip()
        if raw.lower() == "reset":
            cur.execute("DELETE FROM settings WHERE key=?", (key,))
            conn.commit()
            await update.message.reply_text("♻️ <b>Button reset to default.</b>", parse_mode="HTML")
        else:
            value = sanitize_premium_name(raw)
            set_setting(key, value)
            log_admin_action(uid, "EDIT_BUTTON", f"{key} -> {strip_premium(value)[:40]}")
            await update.message.reply_text("✅ <b>Button updated.</b>\n\nOpen the main menu to see it.", parse_mode="HTML")
        return True

    if state and state.startswith("admin_edit_template:") and is_admin(uid):
        key = state.split(":", 1)[1]
        user_state.pop(uid, None)
        body = premium_html(update, text).strip()
        set_message_template(key, body)
        log_admin_action(uid, "EDIT_TEMPLATE", key)
        await update.message.reply_text("✅ <b>Template saved.</b>", parse_mode="HTML")
        return True

    if state and state.startswith("admin_easy_edit:") and is_admin(uid):
        try:
            _, product_id_text, field = state.split(":", 2)
            product_id = int(product_id_text)
            value = text.strip()
            if field in ("name", "description", "validity", "warranty", "rules"):
                # Preserve Telegram premium emojis as <tg-emoji> HTML tags.
                value = premium_html(update, text).strip()
                if field == "name":
                    # Names keep only premium-emoji tags; bold/italic etc. are dropped.
                    value = sanitize_premium_name(value)
            cur.execute("SELECT name, price FROM products WHERE id=?", (product_id,))
            product = cur.fetchone()
            if not product:
                await update.message.reply_text("❌ <b>Product not found.</b>", parse_mode="HTML")
                user_state.pop(uid, None)
                return True
            if field == "price":
                value = float(value)
            allowed = {"name","price","description","validity","warranty","rules"}
            if field not in allowed:
                await update.message.reply_text("❌ <b>Invalid field.</b>", parse_mode="HTML")
                user_state.pop(uid, None)
                return True
            cur.execute(f"UPDATE products SET {field}=? WHERE id=?", (value, product_id))
            conn.commit()
            log_admin_action(uid, "EASY_EDIT_PRODUCT", f"Product #{product_id} field={field}")
            user_state.pop(uid, None)

            # Auto-broadcast price changes to all users.
            broadcast_note = ""
            if field == "price":
                try:
                    old_price = float(product[1] or 0)
                    new_price = float(value)
                    if abs(new_price - old_price) >= 0.005:
                        # Auto-scale bulk (tier) prices by the same ratio as the base price.
                        try:
                            if old_price > 0 and new_price > 0:
                                # Offers hand-typed in the last few minutes are
                                # never rescaled - the admin's numbers win.
                                tiers = [] if tiers_recently_edited(product_id, 5) else get_product_price_tiers(product_id)
                                ratio = new_price / old_price
                                for _tier_id, tier_qty, tier_price in tiers:
                                    # qty-1 offers always follow the base price exactly.
                                    scaled = new_price if int(tier_qty) == 1 else round(float(tier_price) * ratio, 2)
                                    set_product_price_tier(product_id, int(tier_qty), scaled, manual=False)
                                if tiers:
                                    broadcast_note += f"\n\U0001f4e6 Bulk prices auto-updated (<b>{len(tiers)}</b> tiers)"
                        except Exception:
                            broadcast_note += "\n\u26a0\ufe0f Bulk price auto-update failed"
                        available = get_available_stock_count(product_id)
                        sent, failed = await broadcast_price_change(context, product_id, str(product[0]), old_price, new_price, available)
                        broadcast_note += f"\n\U0001f4e2 Price broadcast sent to <b>{sent}</b> users"
                except Exception:
                    broadcast_note = "\n\u26a0\ufe0f Price broadcast failed"

            await update.message.reply_text(f"✅ <b>Product Updated</b>\n\n🆔 Product ID: <code>{product_id}</code>\n✏️ Field: <code>{field}</code>{broadcast_note}", reply_markup=product_edit_options_keyboard(product_id), parse_mode="HTML")
        except Exception as exc:
            await update.message.reply_text(f"❌ <b>Update failed.</b>\n\n<code>{exc}</code>", parse_mode="HTML")
            return True
        return True

    if state and state.startswith("admin_m365_set_slots:") and is_admin(uid):
        try:
            product_id = int(state.split(":")[1])
            cur.execute("SELECT name, price FROM products WHERE id=?", (product_id,))
            product = cur.fetchone()
            if not product:
                await update.message.reply_text("❌ <b>Product not found.</b>", parse_mode="HTML")
                user_state.pop(uid, None)
                return True
            raw = text.strip()
            exact_mode = raw.startswith("=")
            if exact_mode:
                raw = raw[1:].strip()
            if not raw.isdigit():
                await update.message.reply_text("❌ <b>Send a whole number</b> of slots to ADD.\n\nExample: <code>3</code> → adds 3 slots on top of current stock.\nOr <code>=5</code> → sets the total to exactly 5.", parse_mode="HTML")
                return True
            slots = int(raw)
            cur.execute("SELECT COALESCE(stock,0) FROM products WHERE id=?", (product_id,))
            current_stock = int(cur.fetchone()[0] or 0)
            new_total = slots if exact_mode else current_stock + slots
            added = new_total - current_stock
            # Auto-invite products deliver no codes: clear any code rows so the
            # numeric stock column gates purchases, then set it directly.
            cur.execute("DELETE FROM product_stock WHERE product_id=?", (product_id,))
            cur.execute("UPDATE products SET stock=? WHERE id=?", (new_total, product_id))
            conn.commit()
            user_state.pop(uid, None)
            log_admin_action(uid, "M365_SET_SLOTS", f"Product ID {product_id} | added {added} | total {new_total}")

            # Broadcast to all users, same as normal stock additions.
            broadcast_note = ""
            if added > 0:
                try:
                    sent, failed = await broadcast_product_update(context, product_id, str(product[0]), float(product[1] or 0), added, new_total, mode="stock")
                    broadcast_note = f"\n📢 Broadcast sent to <b>{sent}</b> users"
                except Exception:
                    broadcast_note = "\n⚠️ Broadcast failed"

            await update.message.reply_text(f"""✅ <b>Family Slots Updated</b>

📦 Product: <b>{product[0]}</b>
🆔 ID: <code>{product_id}</code>
➕ Added: <code>{added}</code>
🎟 Total Available Slots: <code>{new_total}</code>{broadcast_note}

Customers can now buy this plan. After payment the bot asks their email and sends the Microsoft 365 Family invite automatically.""", reply_markup=products_select_keyboard("admin_stock_select"), parse_mode="HTML")
        except Exception as exc:
            await update.message.reply_text(f"❌ <b>Could not set slots.</b>\n\n<code>{exc}</code>", parse_mode="HTML")
            return True
        return True

    if state and state.startswith("admin_easy_add_stock:") and is_admin(uid):
        try:
            product_id = int(state.split(":")[1])
            cur.execute("SELECT name, price FROM products WHERE id=?", (product_id,))
            product = cur.fetchone()
            if not product:
                await update.message.reply_text("❌ <b>Product not found.</b>", parse_mode="HTML")
                user_state.pop(uid, None)
                return True
            delivery = text.replace("\n", ";;")
            added = restock_product(product_id, delivery)
            available = get_available_stock_count(product_id)
            cur.execute("UPDATE products SET stock=? WHERE id=?", (available, product_id))
            conn.commit()
            product_name, product_price = product
            log_admin_action(uid, "EASY_ADD_STOCK", f"Product ID {product_id} | added {added} | available {available}")
            try:
                sent, failed = await broadcast_product_update(context, product_id, product_name, float(product_price), added, available, mode="stock")
            except Exception:
                sent, failed = 0, 0
            user_state.pop(uid, None)
            await update.message.reply_text(f"""✅ <b>Stock Added</b>

📦 Product: <b>{_admin_name(product_name)}</b>
🆔 ID: <code>{product_id}</code>
➕ Added: <code>{added}</code>
📊 Available Now: <code>{available}</code>
📢 Broadcast: sent to <code>{sent}</code> users""", reply_markup=products_select_keyboard("admin_stock_select"), parse_mode="HTML")
        except Exception as exc:
            await update.message.reply_text(f"❌ <b>Stock add failed.</b>\n\n<code>{exc}</code>", parse_mode="HTML")
            return True
        return True

    if state == "admin_process_withdrawal" and is_admin(uid):
        allowed, wait = admin_action_allowed(uid, "withdrawal_action")
        if not allowed:
            await update.message.reply_text(
                f"⏳ <b>Security Cooldown</b>\n\nPlease wait {wait} seconds before repeating this admin action.",
                parse_mode="HTML"
            )
            return True

        try:
            parts = text.strip().split(maxsplit=2)
            if len(parts) < 2:
                raise ValueError("Missing action")

            withdrawal_id = int(parts[0])
            action = parts[1].upper()
            note = parts[2] if len(parts) > 2 else ""

            if action not in ("APPROVE", "REJECT"):
                raise ValueError("Invalid action")

            status = "approved" if action == "APPROVE" else "rejected"
            result = update_withdrawal_status(withdrawal_id, status, note)

            if not result:
                await update.message.reply_text("❌ <b>Withdrawal request not found.</b>", parse_mode="HTML")
                return True

            target_id, amount, old_status = result

            if old_status != "pending":
                await update.message.reply_text(
                    f"⚠️ <b>Request already processed.</b>\n\nCurrent status: <code>{old_status}</code>",
                    parse_mode="HTML"
                )
                return True

            log_admin_action(uid, f"WITHDRAWAL_{status.upper()}", f"Request #{withdrawal_id} | user {target_id} | {amount:.6f}")

            await update.message.reply_text(
                f"""✅ <b>Withdrawal {status.title()}</b>

🧾 <b>Request:</b> <code>#{withdrawal_id}</code>
👤 <b>User:</b> <code>{target_id}</code>
💰 <b>Amount:</b> <code>{amount:.6f} USDT</code>""",
                reply_markup=admin_menu(),
                parse_mode="HTML"
            )

            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text=(
                        f"💸 <b>Withdrawal {status.title()}</b>\n\n"
                        f"🧾 Request: <code>#{withdrawal_id}</code>\n"
                        f"💰 Amount: <code>{amount:.6f} USDT</code>\n"
                        + (f"📝 Note: {note}" if note else "")
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass

        except Exception:
            await update.message.reply_text(
                """❌ <b>Wrong Format</b>

Use:
<code>REQUEST_ID APPROVE</code>
or
<code>REQUEST_ID REJECT reason</code>""",
                parse_mode="HTML"
            )
            return True

        finally:
            user_state.pop(uid, None)

        return True

    if state == "admin_referral_settings" and is_admin(uid):
        try:
            percent_text, min_text = [x.strip() for x in text.split("|", 1)]
            percent = float(percent_text)
            minimum = float(min_text)

            if percent < 0 or percent > 100:
                raise ValueError("Referral percent must be 0-100")
            if minimum < 0:
                raise ValueError("Minimum cannot be negative")

            set_setting("referral_percent", percent)
            set_setting("min_withdrawal", minimum)
            log_admin_action(uid, "REFERRAL_SETTINGS", f"{percent}% | min {minimum}")

            await update.message.reply_text(
                f"""✅ <b>Referral Settings Updated</b>

📊 Commission: <code>{percent}%</code>
💸 Minimum Withdrawal: <code>{minimum} USDT</code>""",
                reply_markup=admin_menu(),
                parse_mode="HTML"
            )

        except Exception as exc:
            await update.message.reply_text(
                f"""❌ <b>Wrong Format</b>

Use:
<code>5 | 1</code>

Error:
<code>{exc}</code>""",
                parse_mode="HTML"
            )
            return True

        finally:
            user_state.pop(uid, None)

        return True

    if state == "admin_featured_products" and is_admin(uid):
        try:
            product_id_text, action = text.strip().split(maxsplit=1)
            product_id = int(product_id_text)
            action = action.upper()

            if action not in ("ON", "OFF"):
                raise ValueError("Use ON or OFF")

            cur.execute("SELECT name FROM products WHERE id=?", (product_id,))
            product = cur.fetchone()
            if not product:
                await update.message.reply_text("❌ <b>Product not found.</b>", parse_mode="HTML")
                return True

            featured = 1 if action == "ON" else 0
            set_product_featured(product_id, featured)
            log_admin_action(uid, "FEATURED_PRODUCT", f"Product #{product_id} {action}")

            await update.message.reply_text(
                f"""⭐ <b>Featured Product Updated</b>

🆔 Product ID: <code>{product_id}</code>
📦 Product: <b>{product[0]}</b>
⭐ Featured: <b>{action}</b>""",
                reply_markup=admin_menu(),
                parse_mode="HTML"
            )

        except Exception:
            await update.message.reply_text(
                "❌ <b>Wrong Format</b>\n\nUse: <code>PRODUCT_ID ON</code> or <code>PRODUCT_ID OFF</code>",
                parse_mode="HTML"
            )
            return True

        finally:
            user_state.pop(uid, None)

        return True

    if state == "admin_set_product_image" and is_admin(uid):
        try:
            product_id = int(text.strip())
            cur.execute("SELECT name FROM products WHERE id=?", (product_id,))
            product = cur.fetchone()
            if not product:
                await update.message.reply_text("❌ <b>Product not found.</b>", parse_mode="HTML")
                return True

            context.user_data["image_product_id"] = product_id
            user_state[uid] = "admin_wait_product_image"

            await update.message.reply_text(
                f"""🖼 <b>Send Product Image</b>

📦 Product: <b>{product[0]}</b>
🆔 ID: <code>{product_id}</code>

Now send the image/photo.""",
                parse_mode="HTML"
            )

        except Exception:
            await update.message.reply_text(
                "❌ <b>Invalid Product ID.</b>\n\nSend only product ID. Example: <code>12</code>",
                parse_mode="HTML"
            )
            return True

        return True

    if state == "admin_remove_product_image" and is_admin(uid):
        try:
            product_id = int(text.strip())
            remove_product_image(product_id)
            log_admin_action(uid, "REMOVE_PRODUCT_IMAGE", f"Product #{product_id}")

            await update.message.reply_text(
                f"✅ <b>Product image removed.</b>\n\n🆔 Product ID: <code>{product_id}</code>",
                reply_markup=admin_menu(),
                parse_mode="HTML"
            )

        except Exception:
            await update.message.reply_text(
                "❌ <b>Invalid Product ID.</b>\n\nSend only product ID. Example: <code>12</code>",
                parse_mode="HTML"
            )
            return True

        finally:
            user_state.pop(uid, None)

        return True

    if state == "admin_cat_create_name" and is_admin(uid):
        # Same premium-emoji capture as the Add Product wizard: names with
        # Telegram premium emoji are stored with <tg-emoji> tags intact.
        name = sanitize_premium_name(premium_html(update, text).strip())[:120]
        if not strip_premium(name):
            await update.message.reply_text("❌ Please send a category name.", parse_mode="HTML")
            return True
        if get_category_by_name(name):
            await update.message.reply_text(
                "⚠️ <b>A category with this name already exists.</b>\n\nPlease choose another name.",
                parse_mode="HTML",
            )
            return True
        cid = create_category(name)
        admin_temp_data[uid] = {"cat_create_id": cid, "cat_added": []}
        user_state[uid] = "admin_cat_create_products"
        await update.message.reply_text(
            f"""✅ <b>Category name:</b> {_cat_name_html(name)}

Now send the <b>Product IDs</b> that you want to add to this category.

Send one ID — or several at once, separated by commas.

Examples:
<code>30</code>
<code>1,2,3,4,5</code>

📦 <b>Products:</b>
{_products_id_list_text()}""",
            reply_markup=_cat_done_kb(),
            parse_mode="HTML",
        )
        return True

    if state == "admin_cat_create_products" and is_admin(uid):
        datax = admin_temp_data.get(uid, {})
        cat = get_category(datax.get("cat_create_id")) if datax.get("cat_create_id") else None
        if not cat:
            # Diagnostics for the deploy logs: losing the id here means the
            # INSERT's id could not be read back (pgcompat lastrowid on PG).
            log_error(
                "Category create flow lost its category id",
                extra=f"uid={uid} cat_create_id={datax.get('cat_create_id')!r} input={text[:60]!r}",
            )
            user_state.pop(uid, None)
            admin_temp_data.pop(uid, None)
            await update.message.reply_text(
                "❌ <b>Category not found.</b>\nThe category's internal id could not be read back after creation. "
                "Please try again — if it repeats, share the Deploy Logs line 'Category create flow lost its category id'.",
                parse_mode="HTML")
            return True
        await _admin_cat_handle_product_id(update, uid, text, cat, in_create_flow=True)
        return True

    if state == "admin_cat_add_existing" and is_admin(uid):
        datax = admin_temp_data.get(uid, {})
        cat = get_category(datax.get("cat_addex_id")) if datax.get("cat_addex_id") else None
        if not cat:
            user_state.pop(uid, None)
            admin_temp_data.pop(uid, None)
            await update.message.reply_text("❌ <b>Category not found.</b>", parse_mode="HTML")
            return True
        await _admin_cat_handle_product_id(update, uid, text, cat, in_create_flow=False)
        return True

    if state == "admin_cat_rename" and is_admin(uid):
        datax = admin_temp_data.get(uid, {})
        cid = datax.get("cat_rename_id")
        cat = get_category(cid) if cid else None
        new_name = sanitize_premium_name(premium_html(update, text).strip())[:120]
        if not cat:
            user_state.pop(uid, None)
            admin_temp_data.pop(uid, None)
            await update.message.reply_text("❌ <b>Category not found.</b>", parse_mode="HTML")
            return True
        if not strip_premium(new_name):
            await update.message.reply_text("❌ Please send a category name.", parse_mode="HTML")
            return True
        dup = get_category_by_name(new_name)
        if dup and int(dup[0]) != int(cid):
            await update.message.reply_text(
                "⚠️ <b>A category with this name already exists.</b>\n\nPlease choose another name.",
                parse_mode="HTML",
            )
            return True
        old_name = strip_premium(cat[1])
        rename_category(cid, new_name)
        user_state.pop(uid, None)
        admin_temp_data.pop(uid, None)
        log_admin_action(uid, "RENAME_CATEGORY", f"{old_name} -> {new_name}")
        await update.message.reply_text(
            f"""✅ <b>Category renamed successfully.</b>

Old:
{_cat_name_html(old_name)}

New:
{_cat_name_html(new_name)}""",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📂 Open Category", callback_data=f"admin_cat_sel:{cid}")],
                [InlineKeyboardButton("◀️ Back", callback_data="admin_categories")],
            ]),
            parse_mode="HTML",
        )
        return True

    if state == "admin_search_user" and is_admin(uid):
        query = text.strip()
        # Telegram user ID is the canonical identifier: exact, read-only lookup.
        if query.isdigit():
            target = int(query)
            overview = get_user_overview(target)
            if not overview:
                await update.message.reply_text(
                    f"❌ <b>No user found with ID</b> <code>{target}</code>.",
                    reply_markup=admin_menu(),
                    parse_mode="HTML",
                )
                user_state.pop(uid, None)
                return True
            user_state.pop(uid, None)
            await update.message.reply_text(
                _admin_user_card_text(overview),
                reply_markup=admin_user_card_keyboard(target),
                parse_mode="HTML",
            )
            return True

        # Username search (exact case-insensitive first, then partial matches).
        # Users without a username can only be found by their Telegram ID.
        uname = query.lstrip("@")
        if not uname:
            await update.message.reply_text(
                "❌ Send a Telegram user ID or a username.",
                parse_mode="HTML",
            )
            return True
        candidates = find_users_by_username(uname)
        user_state.pop(uid, None)
        if not candidates:
            await update.message.reply_text(
                f"❌ <b>No user found for username</b> <code>@{html.escape(uname, quote=False)}</code>\n\nTip: search by Telegram User ID for an exact match.",
                reply_markup=admin_menu(),
                parse_mode="HTML",
            )
            return True
        if len(candidates) == 1:
            target = candidates[0][0]
            overview = get_user_overview(target)
            await update.message.reply_text(
                _admin_user_card_text(overview),
                reply_markup=admin_user_card_keyboard(target),
                parse_mode="HTML",
            )
            return True
        kb = []
        msg = f"🔎 <b>User Search</b> — <code>@{html.escape(uname, quote=False)}</code>\n\nFound <code>{len(candidates)}</code> users. Choose one:"
        for user_id, first_name, username, balance, blocked, joined in candidates:
            label_name = strip_premium(str(first_name or "User"))[:20]
            label_uname = f"@{username}" if username and username != "No username" else "No username"
            kb.append([InlineKeyboardButton(f"👤 {label_name} • {label_uname} • {user_id}", callback_data=f"admin_usrcard_{user_id}")])
        kb.append([InlineKeyboardButton("🔎 New Search", callback_data="admin_search_user")])
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        return True

    if state == "admin_user_orders" and is_admin(uid):
        try:
            target_id = int(text.strip())
            cur.execute("SELECT first_name, username, balance FROM users WHERE user_id=?", (target_id,))
            user = cur.fetchone()

            cur.execute("""
                SELECT id, product_name, price, created
                FROM orders
                WHERE user_id=?
                ORDER BY id DESC
                LIMIT 30
            """, (target_id,))
            orders = cur.fetchall()

            msg = f"📜 <b>User Orders</b>\n\n🆔 User: <code>{target_id}</code>\n"
            if user:
                first_name, username, balance = user
                uname = f"@{username}" if username and username != "No username" else "No username"
                msg += f"👤 {first_name or 'User'} / {uname}\n💰 Balance: <code>{balance:.2f} USDT</code>\n\n"

            if not orders:
                msg += "No orders found for this user."
            else:
                for order_id, product_name, price, created in orders:
                    msg += f"🧾 <code>#{order_id}</code> — {product_name} — <code>{price:.2f}</code>\n🕒 <code>{created}</code>\n\n"

            if len(msg) > 3900:
                msg = msg[:3900] + "\n..."

            await update.message.reply_text(msg, reply_markup=admin_menu(), parse_mode="HTML")

        except Exception:
            await update.message.reply_text("❌ Send only user ID. Example: <code>7499348138</code>", parse_mode="HTML")
            return True
        finally:
            user_state.pop(uid, None)
        return True

    if state == "admin_resend_order" and is_admin(uid):
        try:
            order_id = int(text.strip())
            cur.execute("SELECT user_id, product_name, price, delivery, created FROM orders WHERE id=?", (order_id,))
            order = cur.fetchone()

            if not order:
                await update.message.reply_text("❌ <b>Order not found.</b>", parse_mode="HTML")
                return True

            target_id, product_name, price, delivery, created = order
            delivery = decrypt_text(delivery)

            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    "🔄 <b>Delivery Resent By Admin</b>\n\n"
                    f"🧾 <b>Order:</b> <code>#{order_id}</code>\n"
                    f"📦 <b>Product:</b> {_admin_name(product_name)}\n"
                    f"💰 <b>Paid:</b> <code>{price:.2f} USDT</code>\n\n"
                    f"🔑 <b>Your Delivery</b>\n<code>{delivery}</code>"
                ),
                parse_mode="HTML"
            )

            log_admin_action(uid, "RESEND_ORDER", f"Order #{order_id} to user {target_id}")

            await update.message.reply_text(
                f"✅ <b>Delivery resent.</b>\n\n🧾 Order: <code>#{order_id}</code>\n👤 User: <code>{target_id}</code>",
                reply_markup=admin_menu(),
                parse_mode="HTML"
            )

        except Exception as exc:
            await update.message.reply_text(
                f"❌ <b>Resend failed.</b>\n\n<code>{exc}</code>",
                parse_mode="HTML"
            )
            return True
        finally:
            user_state.pop(uid, None)
        return True

    if state == "admin_refund_order" and is_admin(uid):
        allowed, wait = admin_action_allowed(uid, "refund_order")
        if not allowed:
            await update.message.reply_text(
                f"⏳ <b>Security Cooldown</b>\n\nPlease wait {wait} seconds before repeating this admin action.",
                parse_mode="HTML"
            )
            return True

        try:
            parts = text.strip().split()
            if len(parts) != 2 or parts[1].upper() != "REFUND":
                raise ValueError("Confirmation word missing")

            order_id = int(parts[0])
            cur.execute("SELECT user_id, product_name, price FROM orders WHERE id=?", (order_id,))
            order = cur.fetchone()

            if not order:
                await update.message.reply_text("❌ <b>Order not found.</b>", parse_mode="HTML")
                return True

            target_id, product_name, price = order
            cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (price, target_id))
            conn.commit()

            log_admin_action(uid, "REFUND_ORDER", f"Order #{order_id} | user {target_id} | amount {price:.2f}")

            await update.message.reply_text(
                f"""✅ <b>Order Refunded</b>

🧾 <b>Order:</b> <code>#{order_id}</code>
👤 <b>User:</b> <code>{target_id}</code>
📦 <b>Product:</b> {_admin_name(product_name)}
💰 <b>Refunded:</b> <code>{price:.2f} USDT</code>""",
                reply_markup=admin_menu(),
                parse_mode="HTML"
            )

            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text=(
                        "💸 <b>Your order was refunded</b>\n\n"
                        f"🧾 <b>Order:</b> <code>#{order_id}</code>\n"
                        f"📦 <b>Product:</b> {_admin_name(product_name)}\n"
                        f"💰 <b>Refund:</b> <code>{price:.2f} USDT</code>\n\n"
                        "Amount was added back to your wallet balance."
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass

        except Exception:
            await update.message.reply_text(
                "❌ <b>Wrong Format</b>\n\nUse: <code>ORDER_ID REFUND</code>",
                parse_mode="HTML"
            )
            return True
        finally:
            user_state.pop(uid, None)
        return True

    if state == "admin_remove_balance" and is_admin(uid):
        allowed, wait = admin_action_allowed(uid, "remove_balance")
        if not allowed:
            await update.message.reply_text(
                f"⏳ <b>Security Cooldown</b>\n\nPlease wait {wait} seconds before repeating this admin action.",
                parse_mode="HTML"
            )
            return True

        try:
            target_id, amount = text.split()
            target_id = int(target_id)
            amount = float(amount)

            cur.execute("SELECT balance FROM users WHERE user_id=?", (target_id,))
            user = cur.fetchone()

            if not user:
                await update.message.reply_text("❌ <b>User not found.</b>", parse_mode="HTML")
                return True

            new_balance = max(0, float(user[0]) - amount)
            cur.execute("UPDATE users SET balance=? WHERE user_id=?", (new_balance, target_id))
            conn.commit()

            log_admin_action(uid, "REMOVE_BALANCE", f"User {target_id} | removed {amount:.2f} | new {new_balance:.2f}")

            await update.message.reply_text(
                f"""✅ <b>Balance Removed</b>

🆔 <b>User:</b> <code>{target_id}</code>
➖ <b>Removed:</b> <code>{amount:.2f} USDT</code>
💰 <b>New Balance:</b> <code>{new_balance:.2f} USDT</code>""",
                reply_markup=admin_menu(),
                parse_mode="HTML"
            )

        except Exception:
            await update.message.reply_text(
                "❌ <b>Wrong Format</b>\n\nUse: <code>USER_ID AMOUNT</code>",
                parse_mode="HTML"
            )
            return True
        finally:
            user_state.pop(uid, None)
        return True

    if state == "admin_add_product_name" and is_admin(uid):
        # Preserve Telegram premium emojis in the name as <tg-emoji> HTML tags.
        admin_temp_data.setdefault(uid, {})["name"] = sanitize_premium_name(premium_html(update, text).strip())
        user_state[uid] = "admin_add_product_price"

        await update.message.reply_text(
            """💰 <b>Step 2/8</b>

Send product price in USDT.

Example:
<code>0.70</code>""",
            reply_markup=admin_menu(),
            parse_mode="HTML"
        )
        return True

    if state == "admin_add_product_price" and is_admin(uid):
        try:
            admin_temp_data.setdefault(uid, {})["price"] = float(text)
            user_state[uid] = "admin_add_product_stock"

            await update.message.reply_text(
                """📦 <b>Step 3/8</b>

Send stock items.

Use <code>;;</code> between each account/key/code.

Example:
<code>code1;;code2;;code3</code>""",
                reply_markup=admin_menu(),
                parse_mode="HTML"
            )
        except Exception:
            await update.message.reply_text("❌ Send only price, example: <code>0.70</code>", parse_mode="HTML")
            return True
        return True

    if state == "admin_add_product_stock" and is_admin(uid):
        items = [x.strip() for x in text.replace(";;", "\n").splitlines() if x.strip()]
        if not items:
            await update.message.reply_text("❌ Send at least one stock item.", parse_mode="HTML")
            return True

        admin_temp_data.setdefault(uid, {})["delivery"] = ";;".join(items)
        admin_temp_data.setdefault(uid, {})["stock"] = len(items)
        user_state[uid] = "admin_add_product_description"

        await update.message.reply_text(
            f"""📝 <b>Step 4/8</b>

Stock detected: <code>{len(items)}</code>

Send product description.

Example:
<code>2 Months Premium Access. Activation link provided.</code>""",
            reply_markup=admin_menu(),
            parse_mode="HTML"
        )
        return True

    if state == "admin_add_product_description" and is_admin(uid):
        admin_temp_data.setdefault(uid, {})["description"] = premium_html(update, text)
        user_state[uid] = "admin_add_product_validity"

        await update.message.reply_text(
            """📌 <b>Step 5/8</b>

Send validity.

Example:
<code>2 Months</code>""",
            reply_markup=admin_menu(),
            parse_mode="HTML"
        )
        return True

    if state == "admin_add_product_validity" and is_admin(uid):
        admin_temp_data.setdefault(uid, {})["validity"] = premium_html(update, text)
        user_state[uid] = "admin_add_product_warranty"

        await update.message.reply_text(
            """🛡 <b>Step 6/8</b>

Send warranty.

Example:
<code>Replacement if not working</code>

If no warranty, send:
<code>No warranty</code>""",
            reply_markup=admin_menu(),
            parse_mode="HTML"
        )
        return True

    if state == "admin_add_product_warranty" and is_admin(uid):
        admin_temp_data.setdefault(uid, {})["warranty"] = premium_html(update, text)
        user_state[uid] = "admin_add_product_rules"

        await update.message.reply_text(
            """📜 <b>Step 7/8</b>

Send rules / terms.

Example:
<code>Only for new users. Do not change account details.</code>""",
            reply_markup=admin_menu(),
            parse_mode="HTML"
        )
        return True

    if state == "admin_add_product_rules" and is_admin(uid):
        admin_temp_data.setdefault(uid, {})["rules"] = premium_html(update, text)
        user_state[uid] = "admin_add_product_redeem"

        await update.message.reply_text(
            """🔗 <b>Step 8/8</b>

Send redeem/activation link or instruction.

Example:
<code>https://example.com/redeem</code>

If not required, send:
<code>Not required</code>""",
            reply_markup=admin_menu(),
            parse_mode="HTML"
        )
        return True

    if state == "admin_add_product_redeem" and is_admin(uid):
        admin_temp_data.setdefault(uid, {})["redeem_link"] = text
        user_state[uid] = "admin_add_product_max_order"

        stock = int(admin_temp_data.get(uid, {}).get("stock", 1))
        suggested = stock if stock <= 10 else 10

        await update.message.reply_text(
            f"""🔢 <b>Step 9/9</b>

Send max quantity per order.

Example:
<code>1</code>

Suggested:
<code>{suggested}</code>""",
            reply_markup=admin_menu(),
            parse_mode="HTML"
        )
        return True

    if state == "admin_add_product_max_order" and is_admin(uid):
        try:
            data = admin_temp_data.get(uid, {})

            name = data["name"]
            price = float(data["price"])
            stock = int(data["stock"])
            delivery = data["delivery"]
            description = data.get("description", "No description added.")
            validity = data.get("validity", "Not mentioned")
            warranty = data.get("warranty", "No warranty")
            rules = data.get("rules", "No rules mentioned")
            redeem_link = data.get("redeem_link") or "Not required"
            max_per_order = int(text)

            if max_per_order < 1:
                max_per_order = 1

            cur.execute(
                """
                INSERT INTO products(name, price, stock, delivery, description, validity, warranty, rules, redeem_link, max_per_order, active)
                VALUES(?,?,?,?,?,?,?,?,?,?,1)
                """,
                (name, price, stock, delivery, description, validity, warranty, rules, redeem_link, max_per_order)
            )
            conn.commit()

            product_id = cur.lastrowid
            added_items = add_stock_items(product_id, delivery) if delivery else 0
            cur.execute("UPDATE products SET stock=? WHERE id=?", (added_items, product_id))
            conn.commit()

            log_admin_action(uid, "ADD_PRODUCT", f"Product #{product_id} | {name} | {price:.2f} USDT | stock {added_items}")

            sent, failed = await broadcast_product_update(
                context,
                product_id,
                name,
                price,
                added_items,
                added_items,
                mode="product",
            )

            await update.message.reply_text(
                f"""✅ <b>Product Added Successfully</b>

🆔 <b>ID:</b> <code>{product_id}</code>
📦 <b>Name:</b> {name}
💰 <b>Price:</b> <code>{price:.2f} USDT</code>
📊 <b>Stock:</b> <code>{added_items}</code>
📌 <b>Validity:</b> {validity}
🛡 <b>Warranty:</b> {warranty}

Product is now live in the shop.

📢 <b>Broadcast:</b> sent to <code>{sent}</code> users.""",
                reply_markup=admin_menu(),
                parse_mode="HTML"
            )

        except Exception as exc:
            await update.message.reply_text(
                f"❌ <b>Product add failed.</b>\n\n<code>{exc}</code>",
                parse_mode="HTML"
            )
            return True

        finally:
            user_state.pop(uid, None)
            admin_temp_data.pop(uid, None)

        return True


    if state == "admin_quick_add_product" and is_admin(uid):
        try:
            parts = [x.strip() for x in text.split("|")]

            if len(parts) < 3:
                raise ValueError("Missing required fields")

            name = parts[0]
            price = float(parts[1])
            delivery = parts[2]
            description = parts[3] if len(parts) > 3 and parts[3] and parts[3] != "-" else "No description added."
            validity = parts[4] if len(parts) > 4 and parts[4] and parts[4] != "-" else "Not mentioned"
            warranty = parts[5] if len(parts) > 5 and parts[5] and parts[5] != "-" else "No warranty"
            rules = parts[6] if len(parts) > 6 and parts[6] and parts[6] != "-" else "No rules mentioned"
            redeem_link = parts[7] if len(parts) > 7 and parts[7] and parts[7] != "-" else "Not required"
            max_per_order = 999

            items = [x.strip() for x in delivery.replace(";;", "\n").splitlines() if x.strip()]
            if not items:
                raise ValueError("No stock items found")

            delivery_joined = ";;".join(items)
            stock = len(items)

            cur.execute(
                """
                INSERT INTO products(name, price, stock, delivery, description, validity, warranty, rules, redeem_link, max_per_order, active)
                VALUES(?,?,?,?,?,?,?,?,?,?,1)
                """,
                (name, price, stock, delivery_joined, description, validity, warranty, rules, redeem_link, max_per_order)
            )
            conn.commit()

            product_id = cur.lastrowid
            added_items = add_stock_items(product_id, delivery_joined)
            cur.execute("UPDATE products SET stock=? WHERE id=?", (added_items, product_id))
            conn.commit()

            log_admin_action(uid, "QUICK_ADD_PRODUCT", f"Product #{product_id} | {name} | stock {added_items}")

            sent, failed = await broadcast_product_update(
                context,
                product_id,
                name,
                price,
                added_items,
                added_items,
                mode="product",
            )

            await update.message.reply_text(
                f"""✅ <b>Product Added Successfully</b>

🆔 <b>ID:</b> <code>{product_id}</code>
📦 <b>Name:</b> {name}
💰 <b>Price:</b> <code>{price:.2f} USDT</code>
📊 <b>Stock:</b> <code>{added_items}</code>
📌 <b>Validity:</b> {validity}
🛡 <b>Warranty:</b> {warranty}
🔢 <b>Max Per Order:</b> <code>{max_per_order}</code>""",
                reply_markup=admin_menu(),
                parse_mode="HTML"
            )

        except Exception as exc:
            await update.message.reply_text(
                f"""❌ <b>Quick Add Failed</b>

<code>{exc}</code>

Use format:
<code>name | price | stock_items | description | validity | warranty | rules | redeem_link</code>""",
                parse_mode="HTML"
            )
            return True

        finally:
            user_state.pop(uid, None)

        return True

    if state == "admin_permanent_delete_product" and is_admin(uid):
        try:
            parts = text.strip().split()
            if len(parts) != 2 or parts[1].upper() != "DELETE":
                raise ValueError("Confirmation word missing")

            pid = int(parts[0])

            cur.execute("SELECT name FROM products WHERE id=?", (pid,))
            product = cur.fetchone()

            if not product:
                await update.message.reply_text(
                    "❌ <b>Product not found.</b>\n\nCheck product ID from Products panel.",
                    parse_mode="HTML"
                )
                return True

            product_name = product[0]

            cur.execute("DELETE FROM product_stock WHERE product_id=?", (pid,))
            cur.execute("DELETE FROM products WHERE id=?", (pid,))
            conn.commit()

            log_admin_action(uid, "PERMANENT_DELETE_PRODUCT", f"Product ID {pid} | {product_name}")

            await update.message.reply_text(
                f"""🗑 <b>Product Permanently Deleted</b>

🆔 <b>Product ID:</b> <code>{pid}</code>
📦 <b>Name:</b> <code>{product_name}</code>

All stock rows for this product were also deleted.""",
                reply_markup=admin_menu(),
                parse_mode="HTML"
            )

        except Exception:
            await update.message.reply_text(
                """❌ <b>Wrong Format</b>

Use:
<code>PRODUCT_ID DELETE</code>

Example:
<code>12 DELETE</code>""",
                parse_mode="HTML"
            )
            return True

        finally:
            user_state.pop(uid, None)

        return True

    if state == "admin_quick_add_product" and is_admin(uid):
        try:
            parts = [x.strip() for x in text.split("|")]
            if len(parts) < 3:
                raise ValueError("Missing required fields")

            name = parts[0]
            price = float(parts[1])
            delivery = parts[2]
            description = parts[3] if len(parts) > 3 and parts[3] and parts[3] != "-" else "No description added."
            validity = parts[4] if len(parts) > 4 and parts[4] and parts[4] != "-" else "Not mentioned"
            warranty = parts[5] if len(parts) > 5 and parts[5] and parts[5] != "-" else "No warranty"
            rules = parts[6] if len(parts) > 6 and parts[6] and parts[6] != "-" else "No rules mentioned"
            redeem_link = parts[7] if len(parts) > 7 and parts[7] and parts[7] != "-" else "Not required"
            max_per_order = 999

            items = [x.strip() for x in delivery.replace(";;", "\n").splitlines() if x.strip()]
            if not items:
                raise ValueError("No stock items found")

            delivery_joined = ";;".join(items)
            stock = len(items)

            cur.execute(
                """
                INSERT INTO products(name, price, stock, delivery, description, validity, warranty, rules, redeem_link, max_per_order, active)
                VALUES(?,?,?,?,?,?,?,?,?,?,1)
                """,
                (name, price, stock, delivery_joined, description, validity, warranty, rules, redeem_link, max_per_order)
            )
            conn.commit()

            product_id = cur.lastrowid
            added_items = add_stock_items(product_id, delivery_joined)
            cur.execute("UPDATE products SET stock=? WHERE id=?", (added_items, product_id))
            conn.commit()

            log_admin_action(uid, "QUICK_ADD_PRODUCT", f"Product #{product_id} | {name} | stock {added_items}")

            sent, failed = await broadcast_product_update(
                context,
                product_id,
                name,
                price,
                added_items,
                added_items,
                mode="product",
            )

            await update.message.reply_text(
                f"""✅ <b>Product Added Successfully</b>

🆔 <b>ID:</b> <code>{product_id}</code>
📦 <b>Name:</b> {name}
💰 <b>Price:</b> <code>{price:.2f} USDT</code>
📊 <b>Stock:</b> <code>{added_items}</code>
📌 <b>Validity:</b> {validity}
🛡 <b>Warranty:</b> {warranty}
🔢 <b>Max Per Order:</b> <code>{max_per_order}</code>""",
                reply_markup=admin_menu(),
                parse_mode="HTML"
            )

        except Exception as exc:
            await update.message.reply_text(
                f"""❌ <b>Quick Add Failed</b>

<code>{exc}</code>

Use:
<code>name | price | stock_items | description | validity | warranty | rules | redeem_link</code>""",
                parse_mode="HTML"
            )
            return True

        finally:
            user_state.pop(uid, None)

        return True

    if state == "admin_search_product" and is_admin(uid):
        try:
            query = text.strip()
            if query.isdigit():
                cur.execute(
                    "SELECT id, name, price, stock, active, description, validity, warranty, max_per_order FROM products WHERE id=?",
                    (int(query),)
                )
            else:
                cur.execute(
                    "SELECT id, name, price, stock, active, description, validity, warranty, max_per_order FROM products WHERE name LIKE ? OR description LIKE ? ORDER BY id DESC LIMIT 20",
                    (f"%{query}%", f"%{query}%")
                )

            rows = cur.fetchall()
            msg = f"🔎 <b>Product Search</b> — <code>{query}</code>\n\n"

            if not rows:
                msg += "No products found."
            else:
                for pid, name, price, old_stock, active, description, validity, warranty, max_per_order in rows:
                    live_stock = get_available_stock_count(pid)
                    stock = live_stock if live_stock > 0 else old_stock
                    status = "🟢 ON" if active else "🔴 HIDDEN"
                    stock_status = "🔴 OUT" if stock <= 0 else ("🟡 LOW" if stock <= 5 else "🟢 OK")
                    msg += (
                        f"🆔 <code>{pid}</code> | {status} | {stock_status}\n"
                        f"📦 <b>{_admin_name(name)}</b>\n"
                        f"💰 <code>{price:.2f} USDT</code> | 📊 <code>{stock}</code> | Max <code>{max_per_order}</code>\n"
                        f"📌 {validity}\n"
                        f"🛡 {warranty}\n\n"
                    )

            if len(msg) > 3900:
                msg = msg[:3900] + "\n..."

            await update.message.reply_text(msg, reply_markup=admin_menu(), parse_mode="HTML")

        finally:
            user_state.pop(uid, None)

        return True

    if state == "admin_bulk_add_stock" and is_admin(uid):
        try:
            product_id_text, delivery = [x.strip() for x in text.split("|", 1)]
            product_id = int(product_id_text)

            cur.execute("SELECT name FROM products WHERE id=?", (product_id,))
            product = cur.fetchone()
            if not product:
                await update.message.reply_text("❌ <b>Product not found.</b>", parse_mode="HTML")
                return True

            added = restock_product(product_id, delivery)
            available = get_available_stock_count(product_id)
            cur.execute("UPDATE products SET stock=? WHERE id=?", (available, product_id))
            conn.commit()

            cur.execute("SELECT name, price FROM products WHERE id=?", (product_id,))
            product_info = cur.fetchone()
            product_name = product_info[0] if product_info else f"Product #{product_id}"
            product_price = float(product_info[1]) if product_info else 0.0

            log_admin_action(uid, "BULK_ADD_STOCK", f"Product ID {product_id} | added {added} | available {available}")

            sent, failed = await broadcast_product_update(
                context,
                product_id,
                product_name,
                product_price,
                added,
                available,
                mode="stock",
            )

            await update.message.reply_text(
                f"""✅ <b>Bulk Stock Added</b>

🆔 <b>Product ID:</b> <code>{product_id}</code>
📦 <b>Product:</b> <code>{product[0]}</code>
➕ <b>Added:</b> <code>{added}</code>
📊 <b>Available Now:</b> <code>{available}</code>
📢 <b>Broadcast:</b> sent to <code>{sent}</code> users""",
                reply_markup=admin_menu(),
                parse_mode="HTML"
            )

        except Exception:
            await update.message.reply_text(
                """❌ <b>Wrong Format</b>

Use:
<code>PRODUCT_ID | item1;;item2;;item3</code>""",
                parse_mode="HTML"
            )
            return True

        finally:
            user_state.pop(uid, None)

        return True

    if state == "admin_duplicate_product" and is_admin(uid):
        try:
            source_id = int(text.strip())
            cur.execute(
                "SELECT name, price, description, validity, warranty, rules, redeem_link, max_per_order FROM products WHERE id=?",
                (source_id,)
            )
            product = cur.fetchone()

            if not product:
                await update.message.reply_text("❌ <b>Product not found.</b>", parse_mode="HTML")
                return True

            name, price, description, validity, warranty, rules, redeem_link, max_per_order = product
            new_name = f"{name} Copy"

            cur.execute(
                """
                INSERT INTO products(name, price, stock, delivery, description, validity, warranty, rules, redeem_link, max_per_order, active)
                VALUES(?,?,?,?,?,?,?,?,?,?,0)
                """,
                (new_name, price, 0, "No stock added yet", description, validity, warranty, rules, redeem_link, max_per_order)
            )
            conn.commit()
            new_id = cur.lastrowid

            log_admin_action(uid, "DUPLICATE_PRODUCT", f"Source {source_id} -> New {new_id}")

            await update.message.reply_text(
                f"""📋 <b>Product Duplicated</b>

✅ <b>New Product ID:</b> <code>{new_id}</code>
📦 <b>Name:</b> <code>{new_name}</code>
💰 <b>Price:</b> <code>{price:.2f} USDT</code>

⚠️ New product is hidden and has 0 stock.
Use Edit Product and Add Stock before making it live.""",
                reply_markup=admin_menu(),
                parse_mode="HTML"
            )

        except Exception:
            await update.message.reply_text(
                "❌ <b>Invalid Product ID</b>\n\nSend only product ID. Example: <code>12</code>",
                parse_mode="HTML"
            )
            return True

        finally:
            user_state.pop(uid, None)

        return True

    if state in ("admin_bulk_hide_products", "admin_bulk_unhide_products") and is_admin(uid):
        allowed, wait = admin_action_allowed(uid, "bulk_hide_unhide")
        if not allowed:
            await update.message.reply_text(
                f"⏳ <b>Security Cooldown</b>\n\nPlease wait {wait} seconds before repeating this admin action.",
                parse_mode="HTML"
            )
            return True

        try:
            ids_text = text.replace(",", " ")
            ids = [int(x) for x in ids_text.split() if x.strip().isdigit()]
            if not ids:
                raise ValueError("No IDs")

            active_value = 0 if state == "admin_bulk_hide_products" else 1
            action_name = "BULK_HIDE_PRODUCTS" if active_value == 0 else "BULK_UNHIDE_PRODUCTS"

            changed = []
            missing = []

            for pid in ids:
                cur.execute("SELECT name FROM products WHERE id=?", (pid,))
                product = cur.fetchone()
                if not product:
                    missing.append(pid)
                    continue
                cur.execute("UPDATE products SET active=? WHERE id=?", (active_value, pid))
                changed.append((pid, product[0]))

            conn.commit()
            log_admin_action(uid, action_name, f"IDs {ids}")

            status_text = "hidden" if active_value == 0 else "visible"
            msg = f"✅ <b>Bulk Update Completed</b>\n\nProducts set to <b>{status_text}</b>:\n"
            for pid, name in changed:
                msg += f"• <code>{pid}</code> — {name}\n"

            if missing:
                msg += "\n⚠️ Missing IDs:\n" + ", ".join([str(x) for x in missing])

            await update.message.reply_text(msg, reply_markup=admin_menu(), parse_mode="HTML")

        except Exception:
            await update.message.reply_text(
                "❌ <b>Wrong Format</b>\n\nSend IDs like: <code>12,15,18</code>",
                parse_mode="HTML"
            )
            return True

        finally:
            user_state.pop(uid, None)

        return True

    if state == "admin_bulk_delete_products" and is_admin(uid):
        allowed, wait = admin_action_allowed(uid, "bulk_delete_products")
        if not allowed:
            await update.message.reply_text(
                f"⏳ <b>Security Cooldown</b>\n\nPlease wait {wait} seconds before repeating this admin action.",
                parse_mode="HTML"
            )
            return True

        try:
            raw = text.strip()
            if "DELETE" not in raw.upper().split():
                raise ValueError("Missing DELETE confirmation")

            clean = raw.replace(",", " ").replace("DELETE", "").replace("delete", "")
            ids = [int(x) for x in clean.split() if x.strip().isdigit()]
            if not ids:
                raise ValueError("No IDs")

            deleted = []
            missing = []

            for pid in ids:
                cur.execute("SELECT name FROM products WHERE id=?", (pid,))
                product = cur.fetchone()
                if not product:
                    missing.append(pid)
                    continue

                product_name = product[0]
                cur.execute("DELETE FROM product_stock WHERE product_id=?", (pid,))
                cur.execute("DELETE FROM products WHERE id=?", (pid,))
                deleted.append((pid, product_name))

            conn.commit()
            log_admin_action(uid, "BULK_PERMANENT_DELETE_PRODUCTS", f"IDs {ids}")

            msg = "🗑 <b>Bulk Delete Completed</b>\n\nDeleted products:\n"
            for pid, name in deleted:
                msg += f"• <code>{pid}</code> — {name}\n"

            if missing:
                msg += "\n⚠️ Missing IDs:\n" + ", ".join([str(x) for x in missing])

            msg += f"\n\n<b>Total Deleted:</b> <code>{len(deleted)}</code>"

            await update.message.reply_text(msg, reply_markup=admin_menu(), parse_mode="HTML")

        except Exception:
            await update.message.reply_text(
                """❌ <b>Wrong Format</b>

Use:
<code>12,15,18 DELETE</code>

Confirmation word <code>DELETE</code> is required.""",
                parse_mode="HTML"
            )
            return True

        finally:
            user_state.pop(uid, None)

        return True

    if state == "admin_edit_product" and is_admin(uid):
        try:
            parts = [x.strip() for x in text.split("|")]

            if len(parts) < 3:
                raise ValueError("Missing fields")

            product_id = int(parts[0])
            new_name = parts[1]
            new_price = float(parts[2])

            cur.execute("SELECT name, price, description, validity, warranty, rules, redeem_link, max_per_order FROM products WHERE id=?", (product_id,))
            old = cur.fetchone()

            if not old:
                await update.message.reply_text(
                    "❌ <b>Product not found.</b>\n\nCheck product ID from Products panel.",
                    parse_mode="HTML"
                )
                return True

            old_name, old_price, old_description, old_validity, old_warranty, old_rules, old_redeem_link, old_max_per_order = old

            description = parts[3] if len(parts) > 3 and parts[3] else old_description
            validity = parts[4] if len(parts) > 4 and parts[4] else old_validity
            warranty = parts[5] if len(parts) > 5 and parts[5] else old_warranty
            rules = parts[6] if len(parts) > 6 and parts[6] else old_rules
            redeem_link = parts[7] if len(parts) > 7 and parts[7] else old_redeem_link
            max_per_order = int(parts[8]) if len(parts) > 8 and parts[8] else old_max_per_order

            cur.execute(
                """
                UPDATE products
                SET name=?, price=?, description=?, validity=?, warranty=?, rules=?, redeem_link=?, max_per_order=?
                WHERE id=?
                """,
                (new_name, new_price, description, validity, warranty, rules, redeem_link, max_per_order, product_id)
            )
            conn.commit()

            log_admin_action(
                uid,
                "EDIT_PRODUCT",
                f"Product #{product_id} | {old_name} ({old_price:.2f}) -> {new_name} ({new_price:.2f})"
            )

            await update.message.reply_text(
                f"""✅ <b>Product Updated</b>



🆔 <b>Product ID:</b>
<code>{product_id}</code>

📦 <b>Name:</b>
<code>{new_name}</code>

💰 <b>Price:</b>
<code>{new_price:.2f} USDT</code>

📌 <b>Validity:</b>
<code>{validity}</code>

🛡 <b>Warranty:</b>
<code>{warranty}</code>

🔢 <b>Max Per Order:</b>
<code>{max_per_order}</code>



<i>Product details have been updated successfully.</i>""",
                reply_markup=admin_menu(),
                parse_mode="HTML"
            )

        except Exception:
            await update.message.reply_text(
                """❌ <b>Wrong Format</b>

Use this format:

<code>PRODUCT_ID | name | price | description | validity | warranty | rules | redeem link</code>

You can also update only name and price:

<code>5 | Netflix Premium 4K | 6.50</code>""",
                parse_mode="HTML"
            )
            return True

        finally:
            user_state.pop(uid, None)

        return True

    if state == "admin_delete_product" and is_admin(uid):
        try:
            pid = int(text)
            cur.execute("UPDATE products SET active=0 WHERE id=?", (pid,))
            conn.commit()
            log_admin_action(uid, "HIDE_PRODUCT", f"Product ID {pid}")

            await update.message.reply_text(
                f"""✅ <b>Product Hidden</b>



🆔 <b>Product ID:</b> <code>{pid}</code>

This product is now hidden from the shop.""",
                reply_markup=admin_menu(),
                parse_mode="HTML"
            )

        except Exception:
            await update.message.reply_text(
                "❌ <b>Invalid Product ID</b>\n\nSend only product ID. Example: <code>12</code>",
                parse_mode="HTML"
            )
            return True

        finally:
            user_state.pop(uid, None)

        return True


    if state == "admin_add_stock" and is_admin(uid):
        try:
            product_id_text, delivery = [x.strip() for x in text.split("|", 1)]
            product_id = int(product_id_text)

            cur.execute("SELECT name FROM products WHERE id=?", (product_id,))
            product = cur.fetchone()

            if not product:
                await update.message.reply_text(
                    "❌ <b>Product not found.</b>\n\nCheck product ID from Products/Inventory panel.",
                    parse_mode="HTML"
                )
                return True

            added = restock_product(product_id, delivery)
            available = get_available_stock_count(product_id)
            cur.execute("SELECT name, price FROM products WHERE id=?", (product_id,))
            product_info = cur.fetchone()
            product_name = product_info[0] if product_info else f"Product #{product_id}"
            product_price = float(product_info[1]) if product_info else 0.0

            log_admin_action(uid, "ADD_STOCK", f"Product ID {product_id} | added {added} | available {available}")

            sent, failed = await broadcast_product_update(
                context,
                product_id,
                product_name,
                product_price,
                added,
                available,
                mode="stock",
            )

            await update.message.reply_text(
                f"""✅ <b>Stock Added</b>



🆔 <b>Product ID:</b>
<code>{product_id}</code>

📦 <b>Product:</b>
<code>{product[0]}</code>

➕ <b>New Items Added:</b>
<code>{added}</code>

✅ <b>Available Stock Now:</b>
<code>{available}</code>



<i>These items are now ready for auto-delivery.</i>""",
                reply_markup=admin_menu(),
                parse_mode="HTML"
            )

        except Exception:
            await update.message.reply_text(
                """❌ <b>Wrong Format</b>

Use this format:

<code>PRODUCT_ID | item1;;item2;;item3</code>

Example:
<code>5 | email1:pass1;;email2:pass2</code>""",
                parse_mode="HTML"
            )
            return True

        finally:
            user_state.pop(uid, None)

        return True


    if state == "admin_add_balance" and is_admin(uid):
        try:
            target_id, amount = text.split()
            target_id = int(target_id)
            amount = float(amount)

            add_balance(target_id, amount)
            log_admin_action(uid, "ADD_BALANCE", f"User {target_id} | amount {amount:.2f} USDT")

            await update.message.reply_text(
                f"""✅ <b>Balance Added</b>



🆔 <b>User ID:</b>
<code>{target_id}</code>

💰 <b>Amount Added:</b>
<code>{amount:.2f} USDT</code>



<i>User wallet balance has been updated.</i>""",
                reply_markup=admin_menu(),
                parse_mode="HTML"
            )

        except Exception:
            await update.message.reply_text(
                """❌ <b>Wrong Format</b>

Use this format:

<code>USER_ID AMOUNT</code>

Example:
<code>7499348138 10</code>""",
                parse_mode="HTML"
            )
            return True

        finally:
            user_state.pop(uid, None)

        return True

    if state == "admin_broadcast" and is_admin(uid):
        import asyncio
        from telegram.error import RetryAfter, Forbidden, TelegramError

        text = premium_html(update, text)
        # Respect admin-blocked users and notification opt-outs (same as the
        # automated broadcasts). Materialise the list now so we don't reuse the
        # shared DB cursor across await points inside the background task.
        cur.execute(
            "SELECT user_id FROM users WHERE COALESCE(blocked,0)=0 AND COALESCE(notifications,1)=1"
        )
        recipients = [row[0] for row in cur.fetchall()]

        user_state.pop(uid, None)

        total = len(recipients)
        body = f"📢 <b>Announcement</b>\n\n{text}"
        admin_chat_id = update.effective_chat.id

        status_msg = await update.message.reply_text(
            f"""📢 <b>Broadcast Started</b>



🎯 <b>Recipients:</b> <code>{total}</code>



<i>Sending in the background — the bot stays fully responsive. You'll get a summary here when it finishes.</i>""",
            reply_markup=admin_menu(),
            parse_mode="HTML"
        )

        async def _run_broadcast():
            sent = 0
            failed = 0
            for idx, target_id in enumerate(recipients, 1):
                try:
                    await context.bot.send_message(
                        chat_id=target_id,
                        text=body,
                        parse_mode="HTML"
                    )
                    sent += 1
                except RetryAfter as exc:
                    # Telegram flood control: wait the requested time, retry once.
                    try:
                        await asyncio.sleep(float(getattr(exc, "retry_after", 5)) + 1)
                        await context.bot.send_message(
                            chat_id=target_id,
                            text=body,
                            parse_mode="HTML"
                        )
                        sent += 1
                    except Exception:
                        failed += 1
                except (Forbidden, TelegramError):
                    failed += 1  # user blocked the bot / deactivated / bad chat
                except Exception:
                    failed += 1

                # Yield to the event loop and stay well under Telegram's
                # ~30 msg/sec global limit so the bot never appears frozen.
                await asyncio.sleep(0.05)

                # Best-effort live progress every 50 messages.
                if idx % 50 == 0:
                    try:
                        await status_msg.edit_text(
                            f"""📢 <b>Broadcasting…</b>

📨 <b>Sent:</b> <code>{sent}</code>
⚠️ <b>Failed:</b> <code>{failed}</code>
⏳ <b>Progress:</b> <code>{idx}/{total}</code>""",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

            log_admin_action(uid, "BROADCAST", f"Sent {sent} | Failed {failed}")

            try:
                await context.bot.send_message(
                    chat_id=admin_chat_id,
                    text=(
                        "✅ <b>Broadcast Completed</b>\n\n\n\n"
                        f"📨 <b>Sent:</b> <code>{sent}</code>\n"
                        f"⚠️ <b>Failed:</b> <code>{failed}</code>\n"
                        f"🎯 <b>Total:</b> <code>{total}</code>\n\n\n\n"
                        "<i>Broadcast process finished.</i>"
                    ),
                    parse_mode="HTML",
                    reply_markup=admin_menu()
                )
            except Exception:
                pass

        context.application.create_task(_run_broadcast())
        return True

    return False



async def block_command(update, context):
    if not is_admin(update.message.from_user.id):
        await update.message.reply_text("❌ <b>Admin only.</b>", parse_mode="HTML")
        return

    try:
        target_id = int(context.args[0])
        block_user(target_id)
        log_admin_action(update.message.from_user.id, "BLOCK_USER", f"User {target_id}")
        await update.message.reply_text(
            f"🚫 <b>User Blocked</b>\n\n🆔 <b>User ID:</b> <code>{target_id}</code>",
            parse_mode="HTML"
        )
    except Exception:
        await update.message.reply_text(
            "❌ <b>Wrong Format</b>\n\nUse: <code>/block USER_ID</code>",
            parse_mode="HTML"
        )


async def unblock_command(update, context):
    if not is_admin(update.message.from_user.id):
        await update.message.reply_text("❌ <b>Admin only.</b>", parse_mode="HTML")
        return

    try:
        target_id = int(context.args[0])
        unblock_user(target_id)
        log_admin_action(update.message.from_user.id, "UNBLOCK_USER", f"User {target_id}")
        await update.message.reply_text(
            f"✅ <b>User Unblocked</b>\n\n🆔 <b>User ID:</b> <code>{target_id}</code>",
            parse_mode="HTML"
        )
    except Exception:
        await update.message.reply_text(
            "❌ <b>Wrong Format</b>\n\nUse: <code>/unblock USER_ID</code>",
            parse_mode="HTML"
        )



async def add_admin_command(update, context):
    uid = update.message.from_user.id

    if not is_owner(uid):
        await update.message.reply_text("❌ <b>Owner only.</b>", parse_mode="HTML")
        return

    try:
        target_id = int(context.args[0])
        add_admin(target_id, uid)
        log_admin_action(uid, "ADD_ADMIN", f"Admin {target_id}")

        await update.message.reply_text(
            f"✅ <b>Admin Added</b>\n\n🆔 User ID: <code>{target_id}</code>",
            parse_mode="HTML"
        )
    except Exception:
        await update.message.reply_text(
            "❌ <b>Wrong Format</b>\n\nUse: <code>/addadmin USER_ID</code>",
            parse_mode="HTML"
        )


async def remove_admin_command(update, context):
    uid = update.message.from_user.id

    if not is_owner(uid):
        await update.message.reply_text("❌ <b>Owner only.</b>", parse_mode="HTML")
        return

    try:
        target_id = int(context.args[0])

        if target_id == uid:
            await update.message.reply_text("❌ <b>You cannot remove the owner.</b>", parse_mode="HTML")
            return

        removed = remove_admin(target_id)

        if removed:
            log_admin_action(uid, "REMOVE_ADMIN", f"Admin {target_id}")
            await update.message.reply_text(
                f"✅ <b>Admin Removed</b>\n\n🆔 User ID: <code>{target_id}</code>",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text("❌ <b>Owner cannot be removed.</b>", parse_mode="HTML")
    except Exception:
        await update.message.reply_text(
            "❌ <b>Wrong Format</b>\n\nUse: <code>/removeadmin USER_ID</code>",
            parse_mode="HTML"
        )


async def admins_command(update, context):
    uid = update.message.from_user.id

    if not is_admin(uid):
        await update.message.reply_text("❌ <b>Admin only.</b>", parse_mode="HTML")
        return

    rows = get_admins()
    text = "🌍 <b>Admins List</b>\n\n"

    for admin_id, first_name, username, added_by, added_at in rows:
        username_text = f"@{username}" if username and username != "No username" else "No username"
        role = "👑 Owner" if is_owner(admin_id) else "🛡 Admin"
        text += (
            f"{role}\n"
            f"🆔 <code>{admin_id}</code>\n"
            f"👤 {first_name or 'User'} / {username_text}\n"
            f"➕ Added by: <code>{added_by}</code>\n"
            f"🕒 <code>{added_at}</code>\n\n"
        )

    await update.message.reply_text(text, parse_mode="HTML")



async def reply_ticket_command(update, context):
    uid = update.message.from_user.id

    if not is_admin(uid):
        await update.message.reply_text("❌ <b>Admin only.</b>", parse_mode="HTML")
        return

    try:
        ticket_id = int(context.args[0])
        reply = " ".join(context.args[1:]).strip()

        if not reply:
            raise ValueError("empty reply")

        ticket = get_ticket(ticket_id)

        if not ticket:
            await update.message.reply_text("❌ <b>Ticket not found.</b>", parse_mode="HTML")
            return

        _, user_id, first_name, username, subject, message, status, old_reply, created, updated = ticket

        reply_ticket(ticket_id, reply)
        log_admin_action(uid, "REPLY_TICKET", f"Ticket #{ticket_id}")

        await update.message.reply_text(
            f"✅ <b>Ticket Replied</b>\n\n🎫 Ticket: <code>#{ticket_id}</code>",
            parse_mode="HTML"
        )

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "👑 <b>Admin replied to your ticket</b>\n\n"
                    f"🎫 <b>Ticket:</b> <code>#{ticket_id}</code>\n"
                    f"📌 <b>Subject:</b> {subject}\n\n"
                    f"💬 <b>Reply:</b>\n{reply}"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass

    except Exception:
        await update.message.reply_text(
            "❌ <b>Wrong Format</b>\n\nUse: <code>/replyticket TICKET_ID your reply</code>",
            parse_mode="HTML"
        )


async def close_ticket_command(update, context):
    uid = update.message.from_user.id

    if not is_admin(uid):
        await update.message.reply_text("❌ <b>Admin only.</b>", parse_mode="HTML")
        return

    try:
        ticket_id = int(context.args[0])
        ticket = get_ticket(ticket_id)

        if not ticket:
            await update.message.reply_text("❌ <b>Ticket not found.</b>", parse_mode="HTML")
            return

        _, user_id, first_name, username, subject, message, status, admin_reply, created, updated = ticket

        close_ticket(ticket_id)
        log_admin_action(uid, "CLOSE_TICKET", f"Ticket #{ticket_id}")

        await update.message.reply_text(
            f"🔒 <b>Ticket Closed</b>\n\n🎫 Ticket: <code>#{ticket_id}</code>",
            parse_mode="HTML"
        )

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "🔒 <b>Your support ticket was closed</b>\n\n"
                    f"🎫 <b>Ticket:</b> <code>#{ticket_id}</code>\n"
                    f"📌 <b>Subject:</b> {subject}"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass

    except Exception:
        await update.message.reply_text(
            "❌ <b>Wrong Format</b>\n\nUse: <code>/closeticket TICKET_ID</code>",
            parse_mode="HTML"
        )
