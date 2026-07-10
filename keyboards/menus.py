import math

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database.db import get_product_price_tiers, cur, is_admin
from config import ADMIN_ID


def main_menu(uid=None):
    rows = [
        [
            InlineKeyboardButton("🛍 Shop", callback_data="shop"),
            InlineKeyboardButton("💰 Deposit", callback_data="deposit")
        ],
        [
            InlineKeyboardButton("👤 Profile", callback_data="profile"),
            InlineKeyboardButton("🆘 Support", callback_data="support_ticket")
        ],
        [
            InlineKeyboardButton("📦 My Orders", callback_data="orders"),
            InlineKeyboardButton("💼 My Wallet", callback_data="wallet")
        ],
        [
            InlineKeyboardButton("🔍 Search", callback_data="search"),
            InlineKeyboardButton("🧹 Clear Chat", callback_data="clear_chat")
        ],
    ]

    try:
        # Show the Admin Panel button for the owner AND any admin promoted
        # via the bot or the web admin panel (admins table).
        if uid is not None and is_admin(uid):
            rows.insert(0, [InlineKeyboardButton("🛡 Admin Panel", callback_data="admin_panel")])
    except Exception:
        pass

    return InlineKeyboardMarkup(rows)


def back_home():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="home")]])


def shop_nav():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="shop"), InlineKeyboardButton("◀️ Back", callback_data="home")]
    ])


def deposit_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧾 Deposit History", callback_data="deposit_history")],
        [InlineKeyboardButton("◀️ Back", callback_data="home")]
    ])


def deposit_invoice_menu(invoice_id=None):
    rows = []
    if invoice_id:
        rows.append([InlineKeyboardButton("❌ Cancel Payment", callback_data=f"canceldep_{invoice_id}")])
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def profile_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 My Stats", callback_data="my_stats")],
        [InlineKeyboardButton("💰 Deposit", callback_data="deposit"), InlineKeyboardButton("🧾 Deposit History", callback_data="deposit_history")],
        [InlineKeyboardButton("🔔 Notifications ON/OFF", callback_data="toggle_notifications")],
        [InlineKeyboardButton("📦 My Orders", callback_data="orders")],
        [InlineKeyboardButton("🎁 Referrals", callback_data="referrals"), InlineKeyboardButton("💸 Withdraw Earnings", callback_data="withdraw_referral")],
        [InlineKeyboardButton("📂 Withdrawal History", callback_data="withdraw_history")],
        [InlineKeyboardButton("🧹 Clear Chat", callback_data="clear_chat")],
        [InlineKeyboardButton("◀️ Back", callback_data="home")],
    ])


def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Smart Dashboard", callback_data="admin_smart_dashboard")],
        [InlineKeyboardButton("📦 Products & Stock", callback_data="admin_section_products")],
        [InlineKeyboardButton("🛒 Orders & Users", callback_data="admin_section_orders_users")],
        [InlineKeyboardButton("📢 Marketing & Growth", callback_data="admin_section_marketing")],
        [InlineKeyboardButton("🛡 Security & System", callback_data="admin_section_system")],
        [InlineKeyboardButton("🎫 Support & Admins", callback_data="admin_section_support")],
        [InlineKeyboardButton("✏️ Message Templates", callback_data="admin_message_templates")],
        [InlineKeyboardButton("🎟 Coupons", callback_data="admin_coupons")],
        [InlineKeyboardButton("🤖 Auto Marketing", callback_data="admin_auto_marketing")],
        [InlineKeyboardButton("◀️ Back", callback_data="home")],
    ])


def admin_products_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Product", callback_data="admin_add_product"), InlineKeyboardButton("⚡ Quick Add", callback_data="admin_quick_add_product")],
        [InlineKeyboardButton("📦 Products", callback_data="admin_products"), InlineKeyboardButton("🔎 Search Product", callback_data="admin_search_product")],
        [InlineKeyboardButton("➕ Add Stock", callback_data="admin_add_stock"), InlineKeyboardButton("🧾 Manage Codes", callback_data="admin_manage_codes")],
        [InlineKeyboardButton("📦 Bulk Add Stock", callback_data="admin_bulk_add_stock")],
        [InlineKeyboardButton("📊 Inventory", callback_data="admin_inventory"), InlineKeyboardButton("⚠️ Low Stock", callback_data="admin_low_stock")],
        [InlineKeyboardButton("🏷 Price Offers", callback_data="admin_price_offers"), InlineKeyboardButton("🔥 Flash Sale", callback_data="admin_flash_sales")],
        [InlineKeyboardButton("📈 Product Analytics", callback_data="admin_product_analytics")],
        [InlineKeyboardButton("✏️ Edit Product", callback_data="admin_edit_product"), InlineKeyboardButton("📋 Duplicate", callback_data="admin_duplicate_product")],
        [InlineKeyboardButton("⭐ Featured", callback_data="admin_featured_products"), InlineKeyboardButton("🖼 Product Image", callback_data="admin_set_product_image")],
        [InlineKeyboardButton("🗑 Remove Image", callback_data="admin_remove_product_image")],
        [InlineKeyboardButton("🙈 Bulk Hide", callback_data="admin_bulk_hide_products"), InlineKeyboardButton("👁 Bulk Unhide", callback_data="admin_bulk_unhide_products")],
        [InlineKeyboardButton("🗑 Bulk Delete", callback_data="admin_bulk_delete_products")],
        [InlineKeyboardButton("⬅️ Admin Home", callback_data="admin_panel")],
    ])


def admin_orders_users_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Orders", callback_data="admin_orders"), InlineKeyboardButton("🔎 Search Orders", callback_data="admin_search_order_menu")],
        [InlineKeyboardButton("🔄 Resend Order", callback_data="admin_resend_order"), InlineKeyboardButton("💸 Refund Order", callback_data="admin_refund_order")],
        [InlineKeyboardButton("👥 Users", callback_data="admin_users"), InlineKeyboardButton("🔎 Search User", callback_data="admin_search_user")],
        [InlineKeyboardButton("🚫 Ban/Unban User", callback_data="admin_ban_unban_menu")],
        [InlineKeyboardButton("📜 User Orders", callback_data="admin_user_orders")],
        [InlineKeyboardButton("💰 Add Balance", callback_data="admin_add_balance"), InlineKeyboardButton("💸 Remove Balance", callback_data="admin_remove_balance")],
        [InlineKeyboardButton("📤 Export Orders", callback_data="admin_export_orders"), InlineKeyboardButton("📤 Export Users", callback_data="admin_export_users")],
        [InlineKeyboardButton("📤 Export Products", callback_data="admin_export_products")],
        [InlineKeyboardButton("⬅️ Admin Home", callback_data="admin_panel")],
    ])


def admin_marketing_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"), InlineKeyboardButton("⏰ Scheduled", callback_data="admin_scheduled_broadcasts")],
        [InlineKeyboardButton("🎟 Coupons", callback_data="admin_coupons")],
        [InlineKeyboardButton("🎁 Bundle Deals", callback_data="admin_bundles")],
        [InlineKeyboardButton("🤖 Auto Marketing", callback_data="admin_auto_marketing")],
        [InlineKeyboardButton("🎁 Referral Settings", callback_data="admin_referral_settings")],
        [InlineKeyboardButton("⭐ Featured Products", callback_data="admin_featured_products")],
        [InlineKeyboardButton("📈 Sales Stats", callback_data="admin_sales_stats")],
        [InlineKeyboardButton("⬅️ Admin Home", callback_data="admin_panel")],
    ])


def admin_system_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Statistics", callback_data="admin_stats_dashboard"), InlineKeyboardButton("🏆 Top Sellers", callback_data="admin_top_sellers")],
        [InlineKeyboardButton("📈 Sales Analytics", callback_data="admin_sales_analytics")],
        [InlineKeyboardButton("🧹 Cleanup", callback_data="admin_cleanup"), InlineKeyboardButton("💾 Create Backup", callback_data="admin_db_backup")],
        [InlineKeyboardButton("📥 Restore Backup", callback_data="admin_restore_backup")],
        [InlineKeyboardButton("🧾 Logs", callback_data="admin_logs")],
        [InlineKeyboardButton("📈 Sales Stats", callback_data="admin_sales_stats")],
        [InlineKeyboardButton("⚠️ Low Stock", callback_data="admin_low_stock")],
        [InlineKeyboardButton("⬅️ Admin Home", callback_data="admin_panel")],
    ])


def admin_support_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎫 Ticket Manager", callback_data="admin_ticket_manager")],
        [InlineKeyboardButton("💸 Withdrawals", callback_data="admin_withdrawals")],
        [InlineKeyboardButton("🌍 Admins", callback_data="admin_manage_admins")],
        [InlineKeyboardButton("⬅️ Admin Home", callback_data="admin_panel")],
    ])



def support_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎫 Create Ticket", callback_data="create_ticket")],
        [InlineKeyboardButton("📂 My Tickets", callback_data="my_tickets")],
        [InlineKeyboardButton("◀️ Back", callback_data="home")],
    ])




def _scaled_offer_button_price(product_id, base_price, tier_qty, tier_price):
    """Display offer buttons with prices scaled from current product price."""
    try:
        tiers = get_product_price_tiers(product_id)
        original_base_price = None

        for _, qty, price in tiers:
            if int(qty) == 1:
                original_base_price = float(price)
                break

        if not original_base_price or original_base_price <= 0:
            return round(float(tier_price), 2)

        return round(float(tier_price) * (float(base_price) / original_base_price), 2)
    except Exception:
        return round(float(tier_price), 2)


def product_buy_menu(product_id, price, stock, max_per_order=999):
    rows = []
    stock = int(stock or 0)
    base_price = float(price or 0)

    if stock > 0:
        tiers = get_product_price_tiers(product_id)

        if tiers:
            for tier_id, qty, tier_price in tiers:
                qty = int(qty)
                if stock >= qty:
                    final_price = _scaled_offer_button_price(product_id, base_price, qty, float(tier_price))
                    icon = "⚡" if qty <= 2 else "🔥" if qty <= 5 else "🚀"
                    rows.append([
                        InlineKeyboardButton(
                            f"{icon} {qty} for ${final_price:.2f}",
                            callback_data=f"previewbuy_{product_id}_{qty}"
                        )
                    ])
        if stock > 1:
            rows.append([InlineKeyboardButton("✨ Custom Quantity", callback_data=f"customqty_{product_id}")])

        rows.append([InlineKeyboardButton("🛒 Buy Now", callback_data=f"previewbuy_{product_id}_1")])
    else:
        rows.append([InlineKeyboardButton("🔔 Notify Me When Back", callback_data=f"notify_stock_{product_id}")])
        rows.append([InlineKeyboardButton("🔄 Refresh Stock", callback_data=f"product_{product_id}")])

    rows.append([InlineKeyboardButton("◀ Back to Shop", callback_data="shop")])

    return InlineKeyboardMarkup(rows)


def quantity_select_menu(product_id, stock):
    rows = []
    stock = int(stock or 0)

    cur.execute("SELECT price FROM products WHERE id=?", (product_id,))
    row = cur.fetchone()
    base_price = float(row[0]) if row else 0.0

    tiers = get_product_price_tiers(product_id)

    if tiers:
        for tier_id, qty, tier_price in tiers:
            qty = int(qty)
            if stock >= qty:
                final_price = _scaled_offer_button_price(product_id, base_price, qty, float(tier_price))
                icon = "⚡" if qty <= 2 else "🔥" if qty <= 5 else "🚀"
                rows.append([
                    InlineKeyboardButton(
                        f"{icon} {qty} for ${final_price:.2f}",
                        callback_data=f"previewbuy_{product_id}_{qty}"
                    )
                ])
    else:
        if stock >= 1:
            rows.append([
                InlineKeyboardButton(
                    f"⚡ 1 for ${base_price:.2f}",
                    callback_data=f"previewbuy_{product_id}_1"
                )
            ])

    if stock > 1:
        rows.append([InlineKeyboardButton("✨ Custom Quantity", callback_data=f"customqty_{product_id}")])

    rows.append([InlineKeyboardButton("◀ Back to Product", callback_data=f"product_{product_id}")])

    return InlineKeyboardMarkup(rows)


def order_preview_menu(product_id, quantity, balance=0, total_price=0, coupon_code=None):
    coupon_row = [InlineKeyboardButton("🎟 Apply Coupon", callback_data=f"checkout_coupon_{product_id}_{quantity}")]
    if coupon_code:
        coupon_row = [
            InlineKeyboardButton(f"🎟 Coupon: {coupon_code}", callback_data=f"checkout_coupon_{product_id}_{quantity}"),
            InlineKeyboardButton("❌ Remove", callback_data=f"checkout_coupon_remove_{product_id}_{quantity}")
        ]

    rows = [
        [InlineKeyboardButton(f"💚 Pay Wallet (${float(balance):.2f})", callback_data=f"buy_{product_id}_{quantity}")],
        [InlineKeyboardButton("⚡ Pay USDT BEP20", callback_data=f"directpay_{product_id}_{quantity}")],
        coupon_row,
        [InlineKeyboardButton("🔢 Change Qty", callback_data=f"qtymenu_{product_id}")],
        [InlineKeyboardButton("⬅️ Back To Product", callback_data=f"product_{product_id}")],
    ]

    # Low-balance smart prompt: offer to deposit exactly what's missing.
    try:
        need = float(total_price) - float(balance)
        if need > 0:
            need = math.ceil(need * 100) / 100.0
            rows.insert(1, [InlineKeyboardButton(f"💰 Deposit ${need:.2f} More", callback_data=f"depneed_{need:.2f}")])
    except Exception:
        pass

    return InlineKeyboardMarkup(rows)


def coupon_apply_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎟 Apply Coupon", callback_data="apply_coupon")],
        [InlineKeyboardButton("🗑 Remove Coupon", callback_data="remove_coupon")],
        [InlineKeyboardButton("⬅️ Back", callback_data="home")],
    ])
