"""Run from the project root (where bot.py is):  python3 verify_patch.py
Verifies ALL patches are fully uploaded (bulk delivery, admin search, deposit
copy buttons, categories, Postgres user-search fix, M365 invite fix)."""
import ast, sys

# function/defs that must exist in each file
CHECKS = {
    "database/db.py": [
        "get_user_overview", "find_user_by_id", "find_users_by_username",
        "search_orders", "count_search_orders", "get_order_admin_detail",
        "get_user_deposits_page", "payment_method_label", "get_order_status_label",
        "get_categories", "get_category", "get_category_by_name", "create_category",
        "rename_category", "set_category_active", "delete_category", "move_category",
        "get_category_counts", "get_category_products", "assign_product_category",
        "set_category_icon", "set_category_sort",
    ],
    "admin/admin_handlers.py": [
        "_admin_user_card_text", "get_order_detail_text", "_show_admin_user_card",
        "_show_admin_user_orders", "_show_admin_user_stats", "_show_admin_user_deposits",
        "handle_admin_message", "handle_admin_callback",
        "admin_categories_menu", "_show_categories_admin", "_show_category_detail",
        "_show_category_products_admin", "_admin_cat_handle_product_id",
        "_products_id_list_text", "_cat_name_html",
    ],
    "products/product_handlers.py": [
        "show_shop", "show_category", "_product_button_rows", "buy_product",
        "start_direct_checkout",
    ],
    "handlers/user_handlers.py": [
        "start", "admin", "buttons", "message_handler", "photo_handler", "document_handler",
    ],
    "keyboards/menus.py": ["admin_menu", "deposit_invoice_menu"],
    "payments/deposit_handlers.py": [
        "handle_copy_invoice", "create_deposit_invoice", "deposit_shortfall", "_invoice_menu_for",
    ],
    "utils/delivery_file.py": [
        "is_bulk_delivery", "split_delivery_items", "build_delivery_txt",
        "send_delivery_txt", "checkout_codes_section",
    ],
    "utils/button_styles.py": ["make_copy_button", "copy_amount_text"],
    "web_admin/app.py": [
        "api_users", "api_user_detail", "api_orders", "api_categories",
        "api_category_create", "api_category_update", "api_category_delete", "api_category_move",
    ],
    "services/m365_family.py": [
        "_find_email_box", "_choose_email_invite", "_open_invite_dialog",
        "_open_invite_anywhere", "send_family_invite_sync",
    ],
}

# plain-text markers that must appear in each file
TEXT_CHECKS = {
    "database/db.py": [
        "user = tuple(user)",                          # Postgres pgcompat slice fix
        '_safe_add_column("products", "category_id"',    # categories migration
        '_safe_add_column("categories", "icon"',         # category icons migration
    ],
    "admin/admin_handlers.py": [
        'list(overview["user"])[:6]',                  # pgcompat-safe card
        "html.escape(str(first_name or 'User')",       # order-detail escaping
        "admin_cat_moveall",                           # batch move confirm
        "sanitize_premium_name(premium_html",           # premium emoji names
        "admin_cat_create_name",                       # create flow state
        "pending_category_id",                         # wizard auto-assign
        "1,2,3,4,5",                                   # multi-ID input hint
    ],
    "products/product_handlers.py": [
        "SHOP_CATEGORY_PAGE_SIZE", "SHOP_PRODUCT_PAGE_SIZE",
        "button_premium(cname)", "shopcat_", "copydep_",
    ],
    "handlers/user_handlers.py": ["shopcat_", "shopcats_", "copydep_", "copymsg_dismiss", "ordview_", "orddl_"],
    "keyboards/menus.py": ['"📂 Categories"', 'callback_data="admin_categories"', "make_copy_button"],
    "payments/deposit_handlers.py": [],
    "payments/bep20_checker.py": ["send_delivery_txt"],
    "payments/trc20_checker.py": ["checkout_codes_section"],
    "payments/binance_checker.py": ["checkout_codes_section"],
    "utils/button_styles.py": ["copy_text"],
    "web_admin/app.py": [
        "per_page", '"pages": pages',                  # users/orders pagination
        "get_category_by_name",                         # duplicate-name rule
        "_order_method_label",                          # admin order search
    ],
    "web_admin/static/js/app.js": [
        "manageCategories", "productCatFilter", "cat_icon_",   # category manager
        "usersGo", "usersLastSearch",                            # users pagination
        "applyOrderFilters", "orderPageGo",                      # orders search/pagination
    ],
    "services/m365_family.py": ["account.microsoft.com/family"],
    "apply_suggested_categories.py": ["SUGGESTED", "--dry-run"],
}

ok = True
paths = list(dict.fromkeys(list(CHECKS) + list(TEXT_CHECKS)))
for path in paths:
    try:
        src = open(path, encoding="utf-8").read()
    except FileNotFoundError:
        ok = False
        print(f"MISSING  {path}")
        continue
    missing = []
    if path.endswith(".py"):
        try:
            tree = ast.parse(src)
            found = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            missing += [n for n in CHECKS.get(path, []) if n not in found]
        except SyntaxError as e:
            ok = False
            print(f"TRUNCATED  {path}  ({e}) -- re-upload fully")
            continue
    missing += [t for t in TEXT_CHECKS.get(path, []) if t not in src]
    if missing:
        ok = False
        print(f"STALE/INCOMPLETE  {path}  (missing: {', '.join(missing)})")
    else:
        print(f"OK   {path}  ({len(src.splitlines())} lines)")

print()
print("ALL FILES OK -- safe to restart the bot" if ok else "Fix the files above, then restart")
sys.exit(0 if ok else 1)
