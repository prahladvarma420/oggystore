"""Run from the project root (where bot.py is):  python3 verify_patch.py
Verifies the ID-list / multi-ID / premium-name patch is fully uploaded."""
import ast, sys

CHECKS = {
    "admin/admin_handlers.py": ["admin_categories_menu", "_show_categories_admin",
                                "_admin_cat_handle_product_id", "_products_id_list_text",
                                "_cat_name_html", "handle_admin_callback",
                                "handle_admin_message"],
    "database/db.py": ["get_category_by_name", "assign_product_category", "get_categories"],
    "products/product_handlers.py": ["show_shop", "show_category"],
}
TEXT_CHECKS = {
    "admin/admin_handlers.py": ["admin_cat_moveall", "sanitize_premium_name(premium_html",
                                "1,2,3,4,5"],
    "products/product_handlers.py": ["button_premium(cname)"],
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
        ok = False; print(f"TRUNCATED  {path}  ({e}) -- re-upload fully")

print()
print("ALL FILES OK -- safe to restart the bot" if ok else "Fix the files above, then restart")
sys.exit(0 if ok else 1)
