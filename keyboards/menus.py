import math

from telegram import InlineKeyboardMarkup
from utils.button_styles import AutoStyledButton as InlineKeyboardButton, make_copy_button, copy_amount_text
from database.db import get_product_price_tiers, cur, is_admin, get_setting
from utils.premium_emoji import button_premium
from config import ADMIN_ID, USDT_BEP20_ADDRESS, USDT_TRC20_ADDRESS, BINANCE_PAY_ID


MAIN_MENU_BUTTONS = [
    ("btn_shop", "🛍 Shop", "shop"),
    ("btn_deposit", "💰 Deposit", "deposit"),
    ("btn_profile", "👤 Profile", "profile"),
    ("btn_support", "🆘 Support", "support_ticket"),
    ("btn_orders", "📦 My Orders", "orders"),
    ("btn_wallet", "💼 My Wallet", "wallet"),
    ("btn_search", "🔍 Search", "search"),
    ("btn_clear_chat", "🧹 Clear Chat", "clear_chat"),
    ("btn_admin_panel", "🛡 Admin Panel", "admin_panel"),
    ("btn_reseller_center", "🤝 Reseller Center", "res_center"),
]

# Buttons reused across dozens of different screens with different callback
# targets each time (a "Back" on the shop screen goes somewhere different
# than a "Back" on the profile screen). They share ONE admin-customizable
# label + emoji each -- edited from the same "🎨 Customize Bot" screen as
# MAIN_MENU_BUTTONS above -- but since the callback differs per call site,
# use back_button(cb) / refresh_button(cb) / deposit_history_button(cb)
# instead of building them from this list directly.
EXTRA_CUSTOM_BUTTONS = [
    ("btn_back_global", "◀️ Back"),
    ("btn_refresh_global", "🔄 Refresh"),
    ("btn_deposit_history", "🧾 Deposit History"),
]


def custom_button(key, default, cb):
    """Main-menu button with an admin-customizable label. The first premium
    emoji in the label becomes the button icon."""
    try:
        label = (get_setting(key, "") or "").strip() or default
    except Exception:
        label = default
    text, icon = button_premium(label)
    return InlineKeyboardButton(text or default, callback_data=cb, icon_custom_emoji_id=icon)


def back_button(callback_data="home"):
    """"◀️ Back" button, reused on every screen that has one. Label/emoji is
    admin-customizable (Customize Bot) and applies everywhere at once."""
    return custom_button("btn_back_global", "◀️ Back", callback_data)


def refresh_button(callback_data):
    """"🔄 Refresh" button, reused on every screen that has one. Label/emoji is
    admin-customizable (Customize Bot) and applies everywhere at once."""
    return custom_button("btn_refresh_global", "🔄 Refresh", callback_data)


def deposit_history_button(callback_data="deposit_history"):
    """"🧾 Deposit History" button. Label/emoji is admin-customizable
    (Customize Bot) and applies everywhere it appears at once."""
    return custom_button("btn_deposit_history", "🧾 Deposit History", callback_data)


def main_menu(uid=None):
    b = {key: custom_button(key, default, cb) for key, default, cb in MAIN_MENU_BUTTONS}
    rows = [
        [b["btn_shop"], b["btn_deposit"]],
        [b["btn_profile"], b["btn_support"]],
        [b["btn_orders"], b["btn_wallet"]],
        [b["btn_search"], b["btn_clear_chat"]],
    ]

    _admin_row = False
    try:
        # Show the Admin Panel button for the owner AND any admin promoted
        # via the bot or the web admin panel (admins table).
        if uid is not None and is_admin(uid):
            rows.insert(0, [b["btn_admin_panel"]])
            _admin_row = True
    except Exception:
        pass

    try:
        # Reseller Center appears ONLY for users who already have a reseller
        # record — regular customers see zero menu change. The application
        # entry point for everyone else is /reseller and the Profile row.
        if uid is not None:
            from database.reseller_db import get_reseller_by_telegram
            if get_reseller_by_telegram(uid):
                rows.insert(1 if _admin_row else 0, [b["btn_reseller_center"]])
    except Exception:
        pass

    return InlineKeyboardMarkup(rows)


def back_home():
    return InlineKeyboardMarkup([[back_button("home")]])


def shop_nav():
    return InlineKeyboardMarkup([
        [refresh_button("shop"), back_button("home")]
    ])


def deposit_menu():
    return InlineKeyboardMarkup([
        [deposit_history_button("deposit_history")],
        [back_button("home")]
    ])


def deposit_invoice_menu(invoice_id=None, method=None, amount=None, note_code=None):
    """Invoice keyboard. When the payment method is known, the first row offers
    native copy-to-clipboard buttons: the value is embedded in the button, so
    tapping it copies instantly (any device) without sending any chat message.
    All values come from the invoice / payment config -- nothing hardcoded."""
    rows = []
    if invoice_id and method in ("bep20", "trc20"):
        address = USDT_BEP20_ADDRESS if method == "bep20" else USDT_TRC20_ADDRESS
        rows.append([
            make_copy_button("📋 Copy Address", address, f"copydep_{invoice_id}_addr"),
            make_copy_button("📋 Copy Exact Amount", copy_amount_text(amount), f"copydep_{invoice_id}_amt"),
        ])
    elif invoice_id and method == "binance":
        rows.append([
            make_copy_button("📋 Copy Binance ID", str(BINANCE_PAY_ID), f"copydep_{invoice_id}_payid"),
            make_copy_button("📋 Copy Caption", str(note_code or ""), f"copydep_{invoice_id}_note"),
        ])
    if invoice_id:
        rows.append([InlineKeyboardButton("❌ Cancel Payment", callback_data=f"canceldep_{invoice_id}")])
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def profile_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 My Stats", callback_data="my_stats")],
        [InlineKeyboardButton("💰 Deposit", callback_data="deposit"), deposit_history_button("deposit_history")],
        [InlineKeyboardButton("🔔 Notifications ON/OFF", callback_data="toggle_notifications")],
        [InlineKeyboardButton("📦 My Orders", callback_data="orders")],
        [InlineKeyboardButton("🤝 Become a Reseller", callback_data="res_center")],
        [InlineKeyboardButton("🎁 Gift Codes", callback_data="gift_codes"), InlineKeyboardButton("🛡 Warranty", callback_data="warranty_menu")],
        [InlineKeyboardButton("🎁 Referrals", callback_data="referrals"), InlineKeyboardButton("💸 Withdraw Earnings", callback_data="withdraw_referral")],
        [InlineKeyboardButton("📂 Withdrawal History", callback_data="withdraw_history")],
        [InlineKeyboardButton("🧹 Clear Chat", callback_data="clear_chat")],
        [back_button("home")],
    ])


def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Smart Dashboard", callback_data="admin_smart_dashboard")],
        [InlineKeyboardButton("📦 Products & Stock", callback_data="admin_section_products")],
        [InlineKeyboardButton("📂 Categories", callback_data="admin_categories")],
        [InlineKeyboardButton("🛒 Orders & Users", callback_data="admin_section_orders_users")],
        [InlineKeyboardButton("📢 Marketing & Growth", callback_data="admin_section_marketing")],
        [InlineKeyboardButton("🛡 Security & System", callback_data="admin_section_system")],
        [InlineKeyboardButton("🎫 Support & Admins", callback_data="admin_section_support")],
        [InlineKeyboardButton("✏️ Message Templates", callback_data="admin_message_templates")],
        [InlineKeyboardButton("🛡 Warranty Requests", callback_data="admin_warranty"), InlineKeyboardButton("🎁 Gift Codes", callback_data="admin_gift")],
        [InlineKeyboardButton("🎟 Coupons", callback_data="admin_coupons")],
        [InlineKeyboardButton("🤖 Auto Marketing", callback_data="admin_auto_marketing")],
        [InlineKeyboardButton("🎨 Customize Bot", callback_data="admin_customize")],
        [InlineKeyboardButton("📩 M365 Auto-Invite", callback_data="admin_m365")],
        [InlineKeyboardButton("🎨 Canva Auto-Invite", callback_data="admin_canva")],
        [back_button("home")],
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
        [back_button("home")],
    ])


def ticket_customer_keyboard(ticket_id):
    """Buttons attached DIRECTLY under an admin's reply message. These are for
    the CUSTOMER: continue or close their own ticket (ownership is verified
    again in the callback handler)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Reply Ticket", callback_data=f"support_reply_{ticket_id}")],
        [InlineKeyboardButton("✅ Close Ticket", callback_data=f"support_close_{ticket_id}")],
    ])


def ticket_admin_keyboard(ticket_id):
    """Buttons for ADMIN ticket notifications/reminders. Reuses the existing
    admin_ticket_* callbacks, which are guarded by is_admin in
    handle_admin_callback."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Reply Ticket", callback_data=f"admin_ticket_reply_{ticket_id}")],
        [InlineKeyboardButton("✅ Close Ticket", callback_data=f"admin_ticket_close_{ticket_id}")],
    ])




def _flash_sale_price(product_id):
    """Active flash-sale price for this product, or None."""
    try:
        cur.execute("SELECT flash_price, flash_ends_at FROM products WHERE id=?", (product_id,))
        row = cur.fetchone()
        if row and row[0] is not None and row[1]:
            from utils.timezone import now
            ends_text = str(row[1]).replace("T", " ")[:19]
            if ends_text > now().strftime("%Y-%m-%d %H:%M:%S"):
                return float(row[0])
    except Exception:
        pass
    return None


def _scaled_offer_button_price(product_id, base_price, tier_qty, tier_price):
    """Display offer buttons with prices scaled from current product price."""
    try:
        # Flash sale: offer buttons are simple multiples of the flash price
        # (e.g. flash $2.50 -> 5 for $12.50), matching what is charged.
        flash = _flash_sale_price(product_id)
        if flash and flash > 0:
            return round(flash * int(tier_qty), 2)

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
        else:
            rows.append([
                InlineKeyboardButton(
                    f"⚡ 1 for ${base_price:.2f}",
                    callback_data=f"previewbuy_{product_id}_1"
                )
            ])

        if stock > 1:
            rows.append([InlineKeyboardButton("✨ Custom Quantity", callback_data=f"customqty_{product_id}")])
    else:
        rows.append([InlineKeyboardButton("🔔 Notify Me When Back", callback_data=f"notify_stock_{product_id}")])
        rows.append([InlineKeyboardButton("🔄 Refresh Stock", callback_data=f"product_{product_id}")])

    rows.append([
        InlineKeyboardButton("💰 Deposit", callback_data="deposit"),
        InlineKeyboardButton("⬅️ Back to Products", callback_data="shop")
    ])

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

    rows.append([
        InlineKeyboardButton("⬅️ Back To Product", callback_data=f"product_{product_id}"),
        InlineKeyboardButton("💰 Deposit", callback_data="deposit"),
    ])

    return InlineKeyboardMarkup(rows)


def order_preview_menu(product_id, quantity, balance=0, total_price=0, coupon_code=None):
    coupon_row = [InlineKeyboardButton("🎟 Apply Coupon", callback_data=f"checkout_coupon_{product_id}_{quantity}")]
    if coupon_code:
        coupon_row = [
            InlineKeyboardButton(f"🎟 Coupon: {coupon_code}", callback_data=f"checkout_coupon_{product_id}_{quantity}"),
            InlineKeyboardButton("❌ Remove", callback_data=f"checkout_coupon_remove_{product_id}_{quantity}")
        ]

    pay_rows = [
        [InlineKeyboardButton(f"💚 Pay Wallet (${float(balance):.2f})", callback_data=f"buy_{product_id}_{quantity}")],
    ]
    # Editable labels (web admin -> Message Templates). Like the main-menu
    # buttons, the FIRST premium emoji in the label becomes the button icon.
    from database.db import (
        get_message_template,
        BTN_CHECKOUT_BEP20_DEFAULT,
        BTN_CHECKOUT_TRC20_DEFAULT,
        BTN_CHECKOUT_BINANCE_DEFAULT,
    )
    from utils.premium_emoji import strip_premium

    def _pay_btn(key, default, cb):
        label = (get_message_template(key, default) or "").strip() or default
        text, icon = button_premium(label)
        return InlineKeyboardButton(text or strip_premium(default), callback_data=cb, icon_custom_emoji_id=icon)

    if isinstance(USDT_BEP20_ADDRESS, str) and USDT_BEP20_ADDRESS.startswith("0x"):
        pay_rows.append([_pay_btn("btn_checkout_bep20", BTN_CHECKOUT_BEP20_DEFAULT, f"directpay_{product_id}_{quantity}")])
    if isinstance(USDT_TRC20_ADDRESS, str) and USDT_TRC20_ADDRESS.startswith("T"):
        pay_rows.append([_pay_btn("btn_checkout_trc20", BTN_CHECKOUT_TRC20_DEFAULT, f"directpaytrc_{product_id}_{quantity}")])
    try:
        from config import BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_PAY_ID
        _binance_ready = bool(BINANCE_API_KEY and BINANCE_API_SECRET and BINANCE_PAY_ID)
    except Exception:
        _binance_ready = False
    if _binance_ready:
        pay_rows.append([_pay_btn("btn_checkout_binance", BTN_CHECKOUT_BINANCE_DEFAULT, f"directpaybnb_{product_id}_{quantity}")])

    rows = pay_rows + [
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
