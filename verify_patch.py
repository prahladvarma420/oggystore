"""Run from the project root (where bot.py is):  python3 verify_patch.py
Verifies all Categories-v3 (in-bot admin) patch files are fully uploaded."""
import ast, sys

CHECKS = {
    "database/db.py": ["get_categories", "create_category", "assign_product_category",
                       "get_category_by_name", "set_category_icon", "set_category_sort",
                       "move_category", "delete_category"],
    "admin/admin_handlers.py": ["admin_categories_menu", "_show_categories_admin",
                                "_show_category_detail", "_show_category_products_admin",
                                "_admin_cat_handle_product_id", "handle_admin_callback",
                                "handle_admin_message"],
    "keyboards/menus.py": ["admin_menu"],
    "web_admin/app.py": ["api_categories", "api_category_create", "api_category_move"],
}
TEXT_CHECKS = {
    "keyboards/menus.py": ['"📂 Categories"', 'callback_data="admin_categories"'],
    "admin/admin_handlers.py": ["admin_cat_create_name", "admin_cat_add_existing",
                                "admin_cat_rename", "pending_category_id"],
    "web_admin/app.py": ["get_category_by_name"],
}

ok = True
for path, names in CHECKS.items():
    try:
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
        found = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        missing = [n for n in names if n not in found]
        missing += [t for t in TEXT_CHECKS.get(path, []) if t not in src]
        if missing:
            ok = False; print(f"STALE/INCOMPLETE  {path}  (missing: {', '.join(missing)})")
        else:
            print(f"OK   {path}  ({len(src.splitlines())} lines)")
    except FileNotFoundError:
        ok = False; print(f"MISSING  {path}")
    except SyntaxError as e:
        ok = False; print(f"TRUNCATED  {path}  (syntax error: {e}) -- re-upload fully")

print()
print("ALL FILES OK -- safe to restart the bot" if ok else "Fix the files above, then restart")
sys.exit(0 if ok else 1)
