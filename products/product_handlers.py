from datetime import datetime
import html
import io
import math
import random
import time
from telegram import InlineKeyboardMarkup
from utils.button_styles import AutoStyledButton as InlineKeyboardButton
from utils.premium_emoji import strip_premium, first_premium_id, button_premium, premium_safe
from database.db import render_message_template,  cur, conn, get_balance, get_available_stock_count, get_sold_stock_count, reserve_stock_item, reserve_stock_items, add_referral_earning, get_product_price_tiers, get_user_coupon, validate_coupon, calculate_coupon_discount, clear_user_coupon, mark_coupon_used, get_setting_value, add_restock_notification, get_restock_wait_count, get_restock_waiters, mark_restock_notified, add_loyalty_points, get_first_purchase_discount, get_buy_x_get_y, get_active_bundles, get_bundle, get_bundle_items, is_m365_product, is_canva_product
from keyboards.menus import main_menu, back_home, shop_nav, product_buy_menu, quantity_select_menu, order_preview_menu
from utils.crypto import encrypt_text
from utils.ui import edit_or_send
from utils.error_logger import log_error
from utils.states import user_state

try:
    from utils.timezone import now
except Exception:
    def now():
        return datetime.now()
from config import ADMIN_ID, LOW_STOCK_ALERT_THRESHOLD, USDT_BEP20_ADDRESS, DEPOSIT_EXPIRE_HOURS, STORE_NAME


# Prevents users from double-clicking Buy Now and buying twice at the same time.
# Timestamp locks auto-expire if a stale lock remains.
PURCHASE_LOCKS = {}
PURCHASE_LOCK_TIMEOUT_SECONDS = 90



async def notify_restock_users(context_bot, product_id: int, product_name: str, available_stock: int):
    """Notify users who requested restock alerts."""
    try:
        if available_stock <= 0:
            return

        waiters = get_restock_waiters(product_id)
        if not waiters:
            return

        sent = 0
        for user_id in waiters:
            try:
                await context_bot.send_message(
                    chat_id=user_id,
                    text=render_message_template(
                        "restock_alert",
                        "🔔 <b>Back In Stock!</b>\n\n📦 <b>{product_name}</b>\n📊 Available: <code>{stock}</code>\n\nTap below to buy now 👇",
                        product_name=premium_safe(product_name),
                        stock=available_stock,
                    ),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🛒 Buy Now", callback_data=f"product_{product_id}")]
                    ]),
                    parse_mode="HTML"
                )
                sent += 1
            except Exception:
                pass

        mark_restock_notified(product_id)
        try:
            await context_bot.send_message(
                chat_id=ADMIN_ID,
                text=f"🔔 <b>Restock notifications sent</b>\n\n📦 {_admin_name(product_name)}\n👥 Sent: <code>{sent}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass

    except Exception as exc:
        log_error("Restock notify failed", exc, extra=f"product={product_id}")


def _admin_name(value):
    """Product name for HTML messages: premium-emoji tags -> fallback emoji,
    other HTML tags removed, then HTML-escaped so stray < > & never break
    parse_mode=HTML."""
    return html.escape(strip_premium(value), quote=False)


async def notify_low_stock(context_bot, product_id: int, product_name: str, remaining_stock: int):
    """Smart low-stock alert: only alert at configured levels like 5,1."""
    try:
        raw_levels = get_setting_value("low_stock_alert_levels", "5,1")
        levels = sorted([int(x.strip()) for x in raw_levels.split(",") if x.strip().isdigit()], reverse=True)
        if not levels:
            levels = [5, 1]

        cur.execute("SELECT low_stock_notified FROM products WHERE id=?", (product_id,))
        row = cur.fetchone()
        last_notified = int(row[0] or 0) if row else 0

        # Pick the TIGHTEST matching level (stock=1 -> alert level 1, not 5).
        matching = [level for level in levels if remaining_stock <= level]
        if not matching:
            return
        alert_level = min(matching)

        # Already alerted at this level or a tighter one -> never send a duplicate.
        if last_notified and last_notified <= alert_level:
            return

        cur.execute("UPDATE products SET low_stock_notified=? WHERE id=?", (alert_level, product_id))
        conn.commit()

        await context_bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "⚠️ <b>Low Stock Alert</b>\n\n"
                f"📦 <b>Product:</b> {_admin_name(product_name)}\n"
                f"🆔 <b>ID:</b> <code>{product_id}</code>\n"
                f"📊 <b>Remaining:</b> <code>{remaining_stock}</code>\n"
                f"🔔 <b>Alert Level:</b> <code>{alert_level}</code>\n\n"
                "➕ Add more stock from Admin Panel."
            ),
            parse_mode="HTML"
        )

    except Exception as exc:
        log_error("Low stock alert failed", exc, extra=f"product={product_id}")


async def notify_admin_order(
    context_bot,
    user_id,
    product_id,
    product_name,
    total_price,
    quantity,
    remaining_stock,
):
    try:
        await context_bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "🛒 <b>New Order Received</b>\n\n"
                f"👤 <b>User ID:</b> <code>{user_id}</code>\n"
                f"📦 <b>Product:</b> {_admin_name(product_name)}\n"
                f"🆔 <b>Product ID:</b> <code>{product_id}</code>\n"
                f"🔢 <b>Quantity:</b> <code>{quantity}</code>\n"
                f"💰 <b>Total:</b> <code>{total_price:.2f} USDT</code>\n"
                f"📊 <b>Remaining Stock:</b> <code>{remaining_stock}</code>"
            ),
            parse_mode="HTML"
        )
    except Exception as exc:
        log_error("Admin order notify failed", exc, extra=f"user={user_id}")


def clean(value, default="Not mentioned"):
    if value is None:
        return default

    value = str(value).strip()
    if not value:
        return default
    # Text captured with premium emojis is stored as ready-to-send HTML
    # (<tg-emoji> tags, already escaped) — display it as-is.
    if "<tg-emoji" in value:
        return value
    # Escape <, > and & so Telegram HTML parse_mode never fails on stored
    # text like "delivery in <7 days" or "<3".
    return html.escape(value, quote=False)



def format_price(price) -> str:
    """$18 for whole numbers, $1.50 otherwise — clean shop-list style."""
    try:
        price = float(price)
    except Exception:
        price = 0.0
    if price == int(price):
        return f"${int(price)}"
    return f"${price:.2f}"


def bulk_discount_lines(product_id: int, display_price, stock: int) -> str:
    """Bulk Discounts block using this product's price offers (qty tiers)."""
    try:
        tiers = get_product_price_tiers(product_id)
        if not tiers:
            return ""

        original_base = None
        for _, tq, tp in tiers:
            if int(tq) == 1:
                original_base = float(tp)
                break

        lines = []
        for _, tq, tp in tiers:
            tq = int(tq)
            if stock >= tq:
                final_tp = float(tp)
                if original_base and original_base > 0:
                    final_tp = round(float(tp) * (float(display_price) / original_base), 2)
                lines.append(f"{tq} for <b>{format_price(final_tp)}</b>")

        if not lines:
            return ""
        return "<b>Bulk Discounts:</b>\n" + "\n".join(lines)
    except Exception:
        return ""


def colorful_stock_badge(stock: int):
    try:
        stock = int(stock or 0)
    except Exception:
        stock = 0

    if stock <= 0:
        return "🔴 Stock: 0"
    if stock <= 2:
        return f"🟠 Stock: {stock}"
    if stock <= 5:
        return f"🟡 Stock: {stock}"
    return f"🟢 Stock: {stock}"


def name_header(name, icon="📦"):
    """Product name headline. Skips the normal-emoji prefix when the name
    already contains a premium emoji so the emoji is not shown twice."""
    if "<tg-emoji" in str(name):
        return f"<b>{name}</b>"
    return f"{icon} <b>{name}</b>"


def colorful_product_icon(name: str):
    name = (name or "").lower()
    if "youtube" in name:
        return "▶️"
    if "chatgpt" in name or "gpt" in name or "openai" in name:
        return "🤖"
    if "canva" in name:
        return "🎨"
    if "linkedin" in name:
        return "💼"
    if "netflix" in name:
        return "🎬"
    if "gemini" in name:
        return "✨"
    if "capcut" in name:
        return "✂️"
    if "vpn" in name or "nord" in name:
        return "🛡"
    if "mail" in name or "outlook" in name:
        return "📧"
    if "notion" in name:
        return "📝"
    if "adobe" in name:
        return "🎭"
    return "📦"


def stock_label(stock: int) -> str:
    if stock <= 0:
        return "🔴 Out"
    return f"🟢 {stock}"


def format_delivery_items(items):
    if isinstance(items, list):
        return "\n\n".join([str(item) for item in items])
    return str(items)


def calculate_total_price(product_id: int, base_price: float, quantity: int) -> float:
    """Calculate total price using best available tier rate proportionally.

    Example: if 10 for $5 exists, custom quantity 13 = 13 * 0.50 = $6.50.
    Uses the cheapest per-item rate among tiers where tier quantity <= requested quantity.
    Falls back to base price if no matching tier exists.
    """
    quantity = int(quantity)
    base_price = float(base_price)

    tiers = get_product_price_tiers(product_id)
    best_unit_price = base_price

    for _, tier_qty, tier_price in tiers:
        tier_qty = int(tier_qty)
        tier_price = float(tier_price)
        if tier_qty <= quantity and tier_qty > 0:
            unit_price = tier_price / tier_qty
            if unit_price < best_unit_price:
                best_unit_price = unit_price

    return round(best_unit_price * quantity, 2)



def get_effective_price(price, flash_price=None, flash_ends_at=None):
    """Apply flash sale price only while the flash sale is active."""
    try:
        if flash_price is not None and flash_ends_at:
            end_time = datetime.strptime(str(flash_ends_at), "%Y-%m-%d %H:%M:%S")
            current_time = datetime.strptime(now().strftime("%Y-%m-%d %H:%M:%S"), "%Y-%m-%d %H:%M:%S")

            if end_time > current_time:
                return float(flash_price), True
    except Exception:
        pass

    return float(price), False


def get_product_basic(product_id: int):
    cur.execute(
        """
        SELECT name, price, stock, flash_price, flash_ends_at
        FROM products
        WHERE id=? AND active=1
        """,
        (product_id,)
    )
    row = cur.fetchone()
    if not row:
        return None
    name, price, stock, flash_price, flash_ends_at = row
    effective_price, _ = get_effective_price(price, flash_price, flash_ends_at)
    return name, effective_price, stock



async def maybe_low_stock_alert(context, product_id: int):
    """Silent admin-only low stock checker."""
    try:
        remaining_stock = get_available_stock_count(product_id)

        cur.execute("SELECT name FROM products WHERE id=?", (product_id,))
        row = cur.fetchone()
        if not row:
            return

        await notify_low_stock(context.bot, product_id, row[0], remaining_stock)
    except Exception as exc:
        log_error("Low stock auto alert failed", exc, extra=f"product={product_id}")


async def show_quantity_menu(q, uid: int, product_id: int):
    product = get_product_basic(product_id)
    if not product:
        await edit_or_send(q, "❌ <b>Product not found.</b>", back_home())
        return

    name, price, old_stock = product
    name = str(name) if "<tg-emoji" in str(name) else html.escape(str(name), quote=False)
    stock = get_available_stock_count(product_id) or old_stock
    balance = get_balance(uid)

    await edit_or_send(
        q,
        f"""{name_header(name)}

🧾 <b>Choose Offer</b>

💰 <b>Base Price:</b> <b>${float(price):.2f}</b>
💼 <b>Your Balance:</b> <b>{balance:.2f} USDT</b>
📊 <b>Available Stock:</b> <b>{stock}</b>

Choose an offer below or use custom quantity:""",
        quantity_select_menu(product_id, stock)
    )


async def show_order_preview(q, uid: int, product_id: int, quantity: int):
    product = get_product_basic(product_id)
    if not product:
        await edit_or_send(q, "❌ <b>Product not found.</b>", back_home())
        return

    name, price, old_stock = product
    name = str(name) if "<tg-emoji" in str(name) else html.escape(str(name), quote=False)
    stock = get_available_stock_count(product_id) or old_stock
    quantity = int(quantity)

    if quantity <= 0:
        quantity = 1

    if quantity > stock:
        await edit_or_send(
            q,
            f"""❌ <b>Not Enough Stock</b>

📦 <b>Product:</b> {name}
📊 <b>Available:</b> <code>{stock}</code>
🔢 <b>Requested:</b> <code>{quantity}</code>""",
            quantity_select_menu(product_id, stock)
        )
        return

    balance = get_balance(uid)
    original_total = calculate_total_price(product_id, float(price), quantity)
    total_price = original_total

    coupon_code = get_user_coupon(uid)
    coupon_line = "🎟 <b>Coupon:</b> None"
    if coupon_code:
        coupon, coupon_error = validate_coupon(coupon_code)
        if coupon:
            discount = calculate_coupon_discount(original_total, coupon)
            total_price = round(original_total - discount, 2)
            coupon_line = f"🎟 <b>Coupon:</b> <b>{coupon_code}</b> — saved <b>${discount:.2f}</b>"
        else:
            clear_user_coupon(uid)
            coupon_code = None
            coupon_line = "🎟 <b>Coupon:</b> Removed expired/invalid coupon"

    unit_price = total_price / quantity if quantity else float(price)
    need = max(0, total_price - balance)

    text = f"""{name_header(name)}

🛒 <b>Checkout Preview</b>

Quantity: <b>{quantity}</b> code{'s' if quantity != 1 else ''}
💰 Rate Used: <b>${unit_price:.2f}</b> each
💵 Original Total: <b>${original_total:.2f}</b>
💼 Final Total: <b>${total_price:.2f}</b>
👛 Wallet Balance: <b>{balance:.2f} USDT</b>
📦 Available Stock: <b>{stock}</b>
{coupon_line}
"""

    if need > 0:
        text += f"\n⚠️ Need <b>{need:.2f} USDT</b> more in your wallet.\n"

    text += "\nChoose how you want to pay:"

    await edit_or_send(q, text, order_preview_menu(product_id, quantity, balance, total_price, coupon_code))


async def show_shop(q):
    cur.execute("SELECT id, name, price, stock, validity, featured FROM products WHERE active=1 ORDER BY featured DESC, id DESC")
    products = cur.fetchall()

    if not products:
        await edit_or_send(
            q,
            "🛍 <b>Products</b>\n\nNo products are available right now.\n\nPlease check again later.",
            shop_nav()
        )
        return

    # One query for all live stock counts (faster than one query per product).
    cur.execute("SELECT product_id, COUNT(*) FROM product_stock WHERE sold=0 GROUP BY product_id")
    live_counts = {row[0]: row[1] for row in cur.fetchall()}

    kb = []
    visible_count = 0

    for pid, name, price, old_stock, validity, featured in products:
        live_stock = live_counts.get(pid, 0)
        stock = live_stock if live_stock > 0 else old_stock

        visible_count += 1
        kb.append([
            InlineKeyboardButton(
                f"{'����� ' if featured else ''}{button_premium(name)[0]} — {format_price(price)} (Stock: {stock})",
                callback_data=f"product_{pid}",
                style="danger" if int(stock or 0) <= 0 else "primary",
                icon_custom_emoji_id=button_premium(name)[1]
            )
        ])

    bundles = get_active_bundles()
    if bundles:
        kb.insert(0, [InlineKeyboardButton("🎁 Bundle Deals", callback_data="bundles")])

    kb.append([
        InlineKeyboardButton("🔄 Refresh", callback_data="shop"),
        InlineKeyboardButton("Back", callback_data="home")
    ])

    await edit_or_send(
        q,
        """🛍 <b>Products</b>

Browse products and get instant delivery.

⚡ Delivery: <b>Automatic</b>
💳 Payment: <b>Wallet Balance</b>

Select a product below:""",
        InlineKeyboardMarkup(kb)
    )


async def show_product(q, pid: int):
    cur.execute(
        """
        SELECT id, name, price, stock, description, validity, warranty, rules, redeem_link, max_per_order, image_file_id, featured, flash_price, flash_ends_at
        FROM products
        WHERE id=? AND active=1
        """,
        (pid,)
    )
    p = cur.fetchone()

    if not p:
        await edit_or_send(
            q,
            "❌ <b>Product not found.</b>\n\nThis product may have been removed.",
            back_home()
        )
        return

    _, name, price, old_stock, description, validity, warranty, rules, redeem_link, max_per_order, image_file_id, featured, flash_price, flash_ends_at = p
    name = str(name) if "<tg-emoji" in str(name) else html.escape(str(name), quote=False)

    description = clean(description, "No description added.")
    validity = clean(validity)
    warranty = clean(warranty, "No warranty")
    rules = clean(rules, "No special rules.")
    redeem_link = clean(redeem_link, "Not required")
    max_per_order = max_per_order or 1

    live_stock = get_available_stock_count(pid)
    stock = live_stock if live_stock > 0 else old_stock

    stock_text = "🟢 Available" if stock > 0 else "🔴 Out of stock"
    display_price, flash_active = get_effective_price(price, flash_price, flash_ends_at)
    sale_text = ""
    if flash_active:
        try:
            ends_dt = datetime.strptime(str(flash_ends_at), "%Y-%m-%d %H:%M:%S")
            remaining = ends_dt - now()
            hours_left = max(1, int(remaining.total_seconds() // 3600))
            sale_text = f"\n🔥 <b>FLASH SALE:</b> <s>${float(price):.2f}</s> ➜ <b>${display_price:.2f}</b>\n⏳ Ends in <b>{hours_left}h</b>"
        except Exception:
            sale_text = f"\n🔥 <b>FLASH SALE:</b> <s>${float(price):.2f}</s> ➜ <b>${display_price:.2f}</b>"

    # Clean product card styled like the reference design.
    offers_block = bulk_discount_lines(pid, display_price, stock)
    sold_count = get_sold_stock_count(pid)

    detail_quote = description
    if validity and validity != "Not mentioned":
        detail_quote += f"\n✅ {validity}"
    if warranty and warranty != "No warranty":
        detail_quote += f"\n🛡 {warranty}"

    text = f"""{name_header(name, colorful_product_icon(name))}

Price: <b>{format_price(display_price)} / code</b>{sale_text}

🔥 <b>{sold_count}</b> sold
"""

    if stock > 0:
        text += f"📦 <b>{stock}</b> in stock\n"

    if stock <= 0:
        text += "\n🔴 <b>Out of stock</b>\n"

    if offers_block:
        text += f"\n{offers_block}\n"

    text += f"\n<blockquote>{detail_quote}</blockquote>\n"

    if rules and rules != "No special rules.":
        text += f"\n<blockquote>📌 {rules}</blockquote>\n"

    text += "\n<i>Delivery is automatic after payment confirmation.</i>"

    if image_file_id:
        try:
            await q.message.reply_photo(
                photo=image_file_id,
                caption=text,
                reply_markup=product_buy_menu(pid, display_price, stock, max_per_order),
                parse_mode="HTML",
            )
            try:
                await q.delete_message()
            except Exception:
                pass
            return
        except Exception:
            pass

    await edit_or_send(
        q,
        text,
        product_buy_menu(pid, display_price, stock, max_per_order)
    )



async def start_direct_checkout(q, uid: int, pid: int, quantity: int = 1):
    """Create a direct USDT BEP20 checkout invoice for a product purchase."""
    product = get_product_basic(pid)
    if not product:
        await edit_or_send(q, "❌ <b>Product not found.</b>", back_home())
        return

    name, price, old_stock = product
    name = str(name) if "<tg-emoji" in str(name) else html.escape(str(name), quote=False)
    stock = get_available_stock_count(pid) or old_stock
    quantity = int(quantity)

    if quantity <= 0:
        quantity = 1

    if quantity > stock:
        await edit_or_send(
            q,
            f"""❌ <b>Not Enough Stock</b>

📦 <b>Product:</b> {name}
📊 <b>Available:</b> <code>{stock}</code>
🔢 <b>Requested:</b> <code>{quantity}</code>""",
            quantity_select_menu(pid, stock)
        )
        return

    total_price = calculate_total_price(pid, float(price), quantity)
    unique_amount = round(total_price + random.randint(100, 999) / 100000, 5)

    # Cancel older pending direct checkout invoices for this user.
    cur.execute(
        "UPDATE deposits SET status='cancelled' WHERE user_id=? AND status='pending'",
        (uid,)
    )

    cur.execute(
        """
        INSERT INTO deposits(user_id, amount, unique_amount, status, checkout_product_id, checkout_quantity, checkout_done)
        VALUES(?,?,?,?,?,?,0)
        """,
        (uid, total_price, unique_amount, "pending", pid, quantity)
    )
    conn.commit()

    cur.execute("SELECT last_insert_rowid()")
    invoice_id = cur.fetchone()[0]

    await edit_or_send(
        q,
        f"""💳 <b>Checkout</b>
━━━━━━━━━━━━━━━━━━━━

{name_header(name)}
🔢 <b>Total Codes:</b> <code>{quantity}</code>
💰 <b>Total:</b> <code>${total_price:.2f}</code>

━━━━━━━━━━━━━━━━━━━━
⚡ <b>USDT BEP20 Auto Instant</b>

💎 <b>Send exactly:</b>
<code>{unique_amount:.5f} USDT</code>

🌐 <b>Network:</b> <b>BNB Smart Chain (BEP20)</b>

🏧 <b>Wallet address:</b>
<code>{USDT_BEP20_ADDRESS}</code>

🧾 <b>Invoice ID:</b> <code>#{invoice_id}</code>
🟠 <b>Status:</b> Waiting for payment
⏳ <b>Expires in:</b> <code>{DEPOSIT_EXPIRE_HOURS} hours</code>

━━━━━━━━━━━━━━━━━━━━
⚠️ Send the exact amount only.
🤖 After confirmation, the bot will deliver automatically.

<b>{STORE_NAME}</b>""",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Check Deposit History", callback_data="deposit_history")],
            [InlineKeyboardButton("⬅️ Back To Product", callback_data=f"product_{pid}")]
        ])
    )


async def buy_product(q, uid: int, pid: int, quantity: int = 1):
    quantity = int(quantity)

    if quantity <= 0:
        quantity = 1

    # Hard safety limit even if callback data is manipulated.
    if quantity > 50:
        quantity = 50

    lock_key = f"{uid}:{pid}"

    now_ts = time.time()
    old_lock = PURCHASE_LOCKS.get(lock_key)

    if old_lock and now_ts - old_lock < PURCHASE_LOCK_TIMEOUT_SECONDS:
        await edit_or_send(
            q,
            "⏳ <b>Order already processing</b>\n\nPlease wait a few seconds. Do not click Buy Now multiple times.",
            back_home()
        )
        return

    PURCHASE_LOCKS[lock_key] = now_ts

    try:
        try:
            await q.edit_message_text(
                "⏳ <b>Processing your order...</b>\n\nPlease wait. Do not click again.",
                parse_mode="HTML"
            )
        except Exception:
            pass

        cur.execute(
            """
            SELECT name, price, stock, delivery, validity, warranty, redeem_link, max_per_order
            FROM products
            WHERE id=? AND active=1
            """,
            (pid,)
        )
        p = cur.fetchone()

        if not p:
            await edit_or_send(
                q,
                "❌ <b>Product not found.</b>\n\nThis product may have been removed.",
                back_home()
            )
            return

        name, price, old_stock, fallback_delivery, validity, warranty, redeem_link, max_per_order = p
        raw_name = str(name)
        name = html.escape(raw_name, quote=False)
        validity = clean(validity)
        warranty = clean(warranty, "No warranty")
        redeem_link = clean(redeem_link, "Not required")
        max_per_order = max_per_order or 1


        balance = get_balance(uid)
        live_stock = get_available_stock_count(pid)
        available_stock = live_stock if live_stock > 0 else old_stock

        if available_stock < quantity:
            await edit_or_send(
                q,
                f"""❌ <b>Not Enough Stock</b>

📦 <b>Product:</b> {name}
📊 <b>Available:</b> {available_stock}
🔢 <b>Requested:</b> {quantity}

Please select a lower quantity or check again later.""",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh Product", callback_data=f"product_{pid}")],
                    [InlineKeyboardButton("⬅️ Back To Products", callback_data="shop")]
                ])
            )
            return

        total_price = calculate_total_price(pid, float(price), quantity)

        coupon_code = get_user_coupon(uid)
        coupon = None
        discount = 0.0

        first_discount_percent = get_first_purchase_discount(uid)
        first_purchase_discount = 0.0
        if first_discount_percent > 0:
            first_purchase_discount = round(total_price * first_discount_percent / 100, 2)
            total_price = round(total_price - first_purchase_discount, 2)

        if coupon_code:
            coupon, coupon_error = validate_coupon(coupon_code)
            if coupon:
                discount = calculate_coupon_discount(total_price, coupon)
                total_price = round(total_price - discount, 2)
            else:
                clear_user_coupon(uid)

        if balance < total_price:
            need = total_price - balance

            await edit_or_send(
                q,
                f"""❌ <b>Insufficient Balance</b>

📦 <b>Product:</b> {name}
🔢 <b>Quantity:</b> {quantity}
💰 <b>Total:</b> {total_price:.2f} USDT
👛 <b>Your Balance:</b> {balance:.2f} USDT
➕ <b>Need More:</b> {math.ceil(need * 100) / 100:.2f} USDT

Deposit exactly what's missing with one tap below.""",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"💰 Deposit ${math.ceil(need * 100) / 100:.2f} Now", callback_data=f"depneed_{math.ceil(need * 100) / 100:.2f}")],
                    [InlineKeyboardButton("💰 Deposit Custom Amount", callback_data="deposit")],
                    [InlineKeyboardButton("⬅️ Back To Products", callback_data="shop")],
                ]),
            )
            return

        # ---------- Microsoft 365 Family auto-invite product ----------
        # No stock codes are delivered. After payment the bot asks the
        # customer's email and sends the M365 Family invite automatically.
        if is_m365_product(pid):
            if quantity != 1:
                await edit_or_send(
                    q,
                    "❌ <b>This plan can only be purchased 1 at a time.</b>\n\nPlease buy quantity 1.",
                    product_buy_menu(pid, price, available_stock, max_per_order)
                )
                return

            cur.execute("UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?", (total_price, uid, total_price))

            if cur.rowcount == 0:
                conn.rollback()
                await edit_or_send(
                    q,
                    "❌ <b>Purchase Failed</b>\n\nYour balance changed while processing this order.\n\nPlease refresh and try again.",
                    main_menu(uid)
                )
                return

            cur.execute("UPDATE products SET stock=stock-1 WHERE id=? AND stock > 0", (pid,))

            cur.execute(
                "INSERT INTO orders(user_id, product_name, price, delivery) VALUES(?,?,?,?)",
                (uid, f"{raw_name} x1", total_price, encrypt_text("Microsoft 365 Family invite — waiting for customer email"))
            )
            conn.commit()
            order_id = cur.lastrowid

            if discount > 0 and coupon_code:
                mark_coupon_used(coupon_code)
                clear_user_coupon(uid)

            add_loyalty_points(uid, total_price)

            referral_earning, referrer_id = add_referral_earning(uid, total_price)
            if referral_earning and referrer_id:
                try:
                    await q.message.get_bot().send_message(
                        chat_id=referrer_id,
                        text=(
                            "🎁 <b>Referral Earning Added</b>\n\n"
                            f"👤 Referred user bought: <b>{_admin_name(name)}</b>\n"
                            f"💰 Earning: <b>{referral_earning:.6f} USDT</b>\n\n"
                            "Use Profile → Referrals to view earnings."
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            new_balance = get_balance(uid)

            cur.execute("SELECT stock FROM products WHERE id=?", (pid,))
            stock_row = cur.fetchone()
            remaining_slots = stock_row[0] if stock_row else 0

            await notify_admin_order(
                q.message.get_bot(),
                uid,
                pid,
                name,
                total_price,
                1,
                remaining_slots,
            )

            user_state[uid] = f"m365_email:{order_id}"

            invite_prompt = (
                "✅ <b>Payment Successful!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🧾 <b>Order ID:</b> <b>#{order_id}</b>\n"
                f"📦 <b>{_admin_name(name)}</b>\n\n"
                f"💰 <b>Paid:</b> <b>${total_price:.2f}</b>\n"
                f"💼 <b>Remaining Balance:</b> <b>${new_balance:.2f}</b>\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📧 <b>Now send the email address</b> where you want your Microsoft 365 invite.\n\n"
                "Example: <code>yourname@gmail.com</code>\n\n"
                "📨 The invite will be sent to that inbox automatically — open it and tap <b>Accept</b>."
            )

            try:
                await q.edit_message_text(invite_prompt, parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                await q.message.reply_text(invite_prompt, parse_mode="HTML", disable_web_page_preview=True)
            return

        # ---------- Canva team auto-invite product ----------
        # No stock codes are delivered. After payment the bot asks the
        # customer's email and sends the Canva team invite automatically.
        if is_canva_product(pid):
            if quantity != 1:
                await edit_or_send(
                    q,
                    "❌ <b>This plan can only be purchased 1 at a time.</b>\n\nPlease buy quantity 1.",
                    product_buy_menu(pid, price, available_stock, max_per_order)
                )
                return

            cur.execute("UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?", (total_price, uid, total_price))

            if cur.rowcount == 0:
                conn.rollback()
                await edit_or_send(
                    q,
                    "❌ <b>Purchase Failed</b>\n\nYour balance changed while processing this order.\n\nPlease refresh and try again.",
                    main_menu(uid)
                )
                return

            cur.execute("UPDATE products SET stock=stock-1 WHERE id=? AND stock > 0", (pid,))

            cur.execute(
                "INSERT INTO orders(user_id, product_name, price, delivery) VALUES(?,?,?,?)",
                (uid, f"{raw_name} x1", total_price, encrypt_text("Canva invite — waiting for customer email"))
            )
            conn.commit()
            order_id = cur.lastrowid

            if discount > 0 and coupon_code:
                mark_coupon_used(coupon_code)
                clear_user_coupon(uid)

            add_loyalty_points(uid, total_price)

            referral_earning, referrer_id = add_referral_earning(uid, total_price)
            if referral_earning and referrer_id:
                try:
                    await q.message.get_bot().send_message(
                        chat_id=referrer_id,
                        text=(
                            "🎁 <b>Referral Earning Added</b>\n\n"
                            f"👤 Referred user bought: <b>{_admin_name(name)}</b>\n"
                            f"💰 Earning: <b>{referral_earning:.6f} USDT</b>\n\n"
                            "Use Profile → Referrals to view earnings."
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            new_balance = get_balance(uid)

            cur.execute("SELECT stock FROM products WHERE id=?", (pid,))
            stock_row = cur.fetchone()
            remaining_slots = stock_row[0] if stock_row else 0

            await notify_admin_order(
                q.message.get_bot(),
                uid,
                pid,
                name,
                total_price,
                1,
                remaining_slots,
            )

            user_state[uid] = f"canva_email:{order_id}"

            invite_prompt = (
                "✅ <b>Payment Successful!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🧾 <b>Order ID:</b> <b>#{order_id}</b>\n"
                f"📦 <b>{_admin_name(name)}</b>\n\n"
                f"💰 <b>Paid:</b> <b>${total_price:.2f}</b>\n"
                f"💼 <b>Remaining Balance:</b> <b>${new_balance:.2f}</b>\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📧 <b>Now send the email address</b> where you want your Canva invite.\n\n"
                "Example: <code>yourname@gmail.com</code>\n\n"
                "📨 The invite will be sent to that inbox automatically — open it and tap the button inside."
            )

            try:
                await q.edit_message_text(invite_prompt, parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                await q.message.reply_text(invite_prompt, parse_mode="HTML", disable_web_page_preview=True)
            return

        deliveries = reserve_stock_items(pid, uid, quantity)

        # Fallback for old products without product_stock rows.
        if deliveries is None:
            if quantity != 1:
                await edit_or_send(
                    q,
                    "❌ <b>Multi-buy not available for this old product stock.</b>\n\nPlease buy 1 item or restock this product from admin panel.",
                    product_buy_menu(pid, price, available_stock, max_per_order)
                )
                return

            delivery_text = fallback_delivery
            cur.execute("UPDATE products SET stock=stock-1 WHERE id=? AND stock > 0", (pid,))
            deliveries = [delivery_text]
        else:
            new_stock = get_available_stock_count(pid)
            cur.execute("UPDATE products SET stock=? WHERE id=?", (new_stock, pid))

        cur.execute("UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?", (total_price, uid, total_price))

        if cur.rowcount == 0:
            conn.rollback()
            await edit_or_send(
                q,
                "❌ <b>Purchase Failed</b>\n\nYour balance changed while processing this order.\n\nPlease refresh and try again.",
                main_menu(uid)
            )
            return

        delivery_text = format_delivery_items(deliveries)

        cur.execute(
            "INSERT INTO orders(user_id, product_name, price, delivery, product_id) VALUES(?,?,?,?,?)",
            (uid, f"{raw_name} x{quantity}", total_price, encrypt_text(delivery_text), pid)
        )
        conn.commit()

        if discount > 0 and coupon_code:
            mark_coupon_used(coupon_code)
            clear_user_coupon(uid)

        loyalty_points = add_loyalty_points(uid, total_price)

        order_id = cur.lastrowid

        referral_earning, referrer_id = add_referral_earning(uid, total_price)
        if referral_earning and referrer_id:
            try:
                await q.message.get_bot().send_message(
                    chat_id=referrer_id,
                    text=(
                        "🎁 <b>Referral Earning Added</b>\n\n"
                        f"👤 Referred user bought: <b>{_admin_name(name)}</b>\n"
                        f"💰 Earning: <b>{referral_earning:.6f} USDT</b>\n\n"
                        "Use Profile → Referrals to view earnings."
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass

        new_balance = get_balance(uid)
        remaining_stock = get_available_stock_count(pid)

        await notify_low_stock(q.message.get_bot(), pid, name, remaining_stock)

        await notify_admin_order(
            q.message.get_bot(),
            uid,
            pid,
            name,
            total_price,
            quantity,
            remaining_stock,
        )

        coupon_line = ""
        if discount > 0 and coupon_code:
            coupon_line = f"\n🎟 <b>Coupon:</b> <b>{coupon_code}</b> saved <b>${discount:.2f}</b>"

        first_discount_line = ""
        if first_purchase_discount > 0:
            first_discount_line = f"\n🎉 <b>First Purchase:</b> saved <b>${first_purchase_discount:.2f}</b>"

        codes_as_file = quantity >= 15

        delivery_message = render_message_template(
            "delivery_success",
            "✅ <b>Payment Successful!</b>\n━━━━━━━━━━━━━━━━━━━━\n\n🧾 <b>Order ID:</b> <b>#{order_id}</b>\n\n📦 <b>{product_name}</b>\n\n🔑 <b>Code(s):</b>\n<code>{delivery}</code>\n\n━━━━━━━━━━━━━━━━━━━━\n\n💰 <b>Paid:</b> <b>${paid}</b>\n💼 <b>Remaining Balance:</b> <b>${balance}</b>\n\n━━━━━━━━━━━━━━━━━━━━\nThank you for your purchase! 🎉\n\nUse /orders to view your order history.",
            order_id=order_id,
            product_name=premium_safe(name),
            delivery=("📎 Your codes are attached as a file below (15+ items)." if codes_as_file else delivery_text),
            paid=f"{total_price:.2f}",
            balance=f"{new_balance:.2f}",
            user=(q.from_user.first_name or ""),
        )

        try:
            await q.edit_message_text(
                delivery_message,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            await q.message.reply_text(
                delivery_message,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        # Large orders: deliver codes as a .txt file instead of a huge message.
        if codes_as_file:
            try:
                codes_file = io.BytesIO(delivery_text.encode("utf-8"))
                codes_file.name = f"order_{order_id}_codes.txt"
                await q.message.reply_document(
                    document=codes_file,
                    filename=f"order_{order_id}_codes.txt",
                    caption=f"🔑 Codes for Order #{order_id} ({quantity} items)"
                )
            except Exception as file_exc:
                log_error("Delivery file send failed", file_exc, extra=f"order={order_id}")
                chunk_size = 3500
                for chunk_start in range(0, len(delivery_text), chunk_size):
                    await q.message.reply_text(
                        f"<code>{html.escape(delivery_text[chunk_start:chunk_start + chunk_size], quote=False)}</code>",
                        parse_mode="HTML",
                    )

        await q.message.reply_text(
            "🏠 <b>Main Menu</b>",
            reply_markup=main_menu(uid),
            parse_mode="HTML",
        )


    except Exception as exc:
        conn.rollback()
        log_error("Purchase error", exc, extra=f"user={uid} product={pid} qty={quantity}")
        await edit_or_send(
            q,
            f"""❌ <b>Purchase Error</b>

Something went wrong while processing your order.

<code>{exc}</code>

If balance was deducted, contact admin immediately.""",
            main_menu(uid)
        )

    finally:
        PURCHASE_LOCKS.pop(lock_key, None)
        try:
            class _Ctx:
                pass
            _ctx = _Ctx()
            _ctx.bot = q.message.get_bot()
            await maybe_low_stock_alert(_ctx, pid)
        except Exception:
            pass


# ============================ BUNDLE DEALS ============================

async def show_bundles(q):
    bundles = get_active_bundles()

    if not bundles:
        await edit_or_send(
            q,
            "🎁 <b>Bundle Deals</b>\n\nNo bundles available right now.\n\nCheck back soon!",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back To Products", callback_data="shop")]])
        )
        return

    kb = []
    for bid, bname, bprice in bundles:
        kb.append([InlineKeyboardButton(f"🎁 {bname} • ${float(bprice):.2f}", callback_data=f"bundle_{bid}")])
    kb.append([InlineKeyboardButton("⬅️ Back To Products", callback_data="shop")])

    await edit_or_send(
        q,
        "🎁 <b>Bundle Deals</b>\n\nGet more for less — combo packs at a special price.\n\nSelect a bundle below:",
        InlineKeyboardMarkup(kb)
    )


async def show_bundle_details(q, uid: int, bundle_id: int):
    bundle = get_bundle(bundle_id)

    if not bundle or not bundle[3]:
        await edit_or_send(q, "❌ <b>Bundle not found.</b>", back_home())
        return

    _bid, bname_raw, bprice, _active = bundle
    bname = _admin_name(bname_raw)
    bprice = float(bprice)
    items = get_bundle_items(bundle_id)

    if not items:
        await edit_or_send(q, "❌ <b>This bundle has no products yet.</b>", back_home())
        return

    balance = get_balance(uid)
    total_value = 0.0
    available = True
    item_lines = ""

    for product_id, item_qty, pname, pprice in items:
        pname_safe = html.escape(str(pname), quote=False)
        worth = float(pprice) * int(item_qty)
        total_value += worth
        enough = get_available_stock_count(product_id) >= int(item_qty)
        if not enough:
            available = False
        mark = "✅" if enough else "🔴"
        item_lines += f"{mark} <b>{item_qty}×</b> {pname_safe} <i>(worth ${worth:.2f})</i>\n"

    savings = max(0.0, total_value - bprice)

    text = f"""🎁 <b>{bname}</b>

{item_lines}
💵 Total value: <s>${total_value:.2f}</s>
💎 Bundle price: <b>${bprice:.2f}</b>
🤑 You save: <b>${savings:.2f}</b>

👛 Your balance: <b>{balance:.2f} USDT</b>"""

    kb = []
    if available:
        kb.append([InlineKeyboardButton(f"💚 Buy Bundle (${bprice:.2f})", callback_data=f"buybundle_{bundle_id}")])
        if balance < bprice:
            need = math.ceil((bprice - balance) * 100) / 100.0
            kb.append([InlineKeyboardButton(f"💰 Deposit ${need:.2f} More", callback_data=f"depneed_{need:.2f}")])
    else:
        text += "\n\n🔴 <b>Some items are out of stock right now.</b>"
    kb.append([InlineKeyboardButton("⬅️ Back To Bundles", callback_data="bundles")])

    await edit_or_send(q, text, InlineKeyboardMarkup(kb))


async def buy_bundle(q, uid: int, bundle_id: int):
    lock_key = f"{uid}:bundle:{bundle_id}"
    now_ts = time.time()
    old_lock = PURCHASE_LOCKS.get(lock_key)

    if old_lock and now_ts - old_lock < PURCHASE_LOCK_TIMEOUT_SECONDS:
        await edit_or_send(
            q,
            "⏳ <b>Order already processing</b>\n\nPlease wait a few seconds. Do not click Buy multiple times.",
            back_home()
        )
        return

    PURCHASE_LOCKS[lock_key] = now_ts

    try:
        bundle = get_bundle(bundle_id)
        if not bundle or not bundle[3]:
            await edit_or_send(q, "❌ <b>Bundle not found.</b>", back_home())
            return

        _bid, bname_raw, bprice, _active = bundle
        bname = _admin_name(bname_raw)
        bprice = float(bprice)
        items = get_bundle_items(bundle_id)

        if not items:
            await edit_or_send(q, "❌ <b>This bundle has no products yet.</b>", back_home())
            return

        # Verify stock for every component before reserving anything.
        for product_id, item_qty, pname, _pprice in items:
            if get_available_stock_count(product_id) < int(item_qty):
                await edit_or_send(
                    q,
                    f"❌ <b>Not Enough Stock</b>\n\n<b>{html.escape(str(pname), quote=False)}</b> is out of stock for this bundle.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back To Bundles", callback_data="bundles")]])
                )
                return

        balance = get_balance(uid)

        if balance < bprice:
            need = math.ceil((bprice - balance) * 100) / 100.0
            await edit_or_send(
                q,
                f"""❌ <b>Insufficient Balance</b>

🎁 <b>Bundle:</b> {bname}
💰 <b>Total:</b> {bprice:.2f} USDT
👛 <b>Your Balance:</b> {balance:.2f} USDT
➕ <b>Need More:</b> {need:.2f} USDT""",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"💰 Deposit ${need:.2f} Now", callback_data=f"depneed_{need:.2f}")],
                    [InlineKeyboardButton("⬅️ Back To Bundles", callback_data="bundles")],
                ]),
            )
            return

        # Reserve stock from every component product.
        sections = []
        total_codes = 0
        for product_id, item_qty, pname, _pprice in items:
            deliveries = reserve_stock_items(product_id, uid, int(item_qty))
            if deliveries is None:
                conn.rollback()
                await edit_or_send(
                    q,
                    "❌ <b>Purchase Failed</b>\n\nStock changed while processing. Please try again.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back To Bundles", callback_data="bundles")]])
                )
                return
            new_stock = get_available_stock_count(product_id)
            cur.execute("UPDATE products SET stock=? WHERE id=?", (new_stock, product_id))
            total_codes += len(deliveries)
            sections.append((str(pname), format_delivery_items(deliveries)))

        cur.execute("UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?", (bprice, uid, bprice))

        if cur.rowcount == 0:
            conn.rollback()
            await edit_or_send(
                q,
                "❌ <b>Purchase Failed</b>\n\nYour balance changed while processing this order.\n\nPlease refresh and try again.",
                main_menu(uid)
            )
            return

        delivery_text = ""
        for pname, section_text in sections:
            delivery_text += f"📦 {pname}:\n{section_text}\n\n"
        delivery_text = delivery_text.strip()

        cur.execute(
            "INSERT INTO orders(user_id, product_name, price, delivery) VALUES(?,?,?,?)",
            (uid, f"🎁 {bname_raw} (bundle)", bprice, encrypt_text(delivery_text))
        )
        conn.commit()
        order_id = cur.lastrowid

        add_loyalty_points(uid, bprice)
        new_balance = get_balance(uid)

        codes_as_file = total_codes >= 15
        if codes_as_file:
            codes_block = "📎 Your codes are attached as a file below (15+ items)."
        else:
            codes_block = f"<code>{html.escape(delivery_text, quote=False)}</code>"

        message = f"""✅ <b>Payment Successful!</b>
━━━━━━━━━━━━━━━━━━━━

🧾 <b>Order ID:</b> <b>#{order_id}</b>

🎁 <b>{bname}</b>

🔑 <b>Code(s):</b>
{codes_block}

━━━━━━━━━━━━━━━━━━━━

💰 <b>Paid:</b> <b>${bprice:.2f}</b>
💼 <b>Remaining Balance:</b> <b>${new_balance:.2f}</b>

Thank you for your purchase! 🎉"""

        try:
            await q.edit_message_text(message, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            await q.message.reply_text(message, parse_mode="HTML", disable_web_page_preview=True)

        if codes_as_file:
            try:
                codes_file = io.BytesIO(delivery_text.encode("utf-8"))
                codes_file.name = f"order_{order_id}_codes.txt"
                await q.message.reply_document(
                    document=codes_file,
                    filename=f"order_{order_id}_codes.txt",
                    caption=f"🔑 Codes for Order #{order_id}"
                )
            except Exception as file_exc:
                log_error("Bundle file send failed", file_exc, extra=f"order={order_id}")
                chunk_size = 3500
                for chunk_start in range(0, len(delivery_text), chunk_size):
                    await q.message.reply_text(
                        f"<code>{html.escape(delivery_text[chunk_start:chunk_start + chunk_size], quote=False)}</code>",
                        parse_mode="HTML",
                    )

        try:
            await notify_admin_order(
                q.message.get_bot(),
                uid,
                0,
                f"🎁 {bname_raw} (bundle)",
                bprice,
                1,
                0,
            )
        except Exception:
            pass

        await q.message.reply_text(
            "🏠 <b>Main Menu</b>",
            reply_markup=main_menu(uid),
            parse_mode="HTML",
        )

    except Exception as exc:
        conn.rollback()
        log_error("Bundle purchase error", exc, extra=f"user={uid} bundle={bundle_id}")
        await edit_or_send(
            q,
            f"""❌ <b>Purchase Error</b>

Something went wrong while processing your bundle order.

<code>{exc}</code>

If balance was deducted, contact admin immediately.""",
            main_menu(uid)
        )

    finally:
        PURCHASE_LOCKS.pop(lock_key, None)
