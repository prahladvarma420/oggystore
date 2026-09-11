"""Run from the project root (where bot.py is):  python3 verify_patch.py
Verifies all Issue-4 patch files are fully and correctly uploaded."""
import ast, sys

CHECKS = {
    "database/db.py": ["get_categories", "create_category", "assign_product_category",
                       "get_category_products", "move_category", "delete_category"],
    "products/product_handlers.py": ["show_shop", "show_category", "_product_button_rows"],
    "handlers/user_handlers.py": ["start", "admin", "buttons", "message_handler",
                                  "photo_handler", "document_handler"],
    "web_admin/app.py": ["api_categories", "api_category_create", "api_category_move"],
}

ok = True
for path, names in CHECKS.items():
    try:
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
        found = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        missing = [n for n in names if n not in found]
        if path == "handlers/user_handlers.py" and "shopcat_" not in src:
            missing.append("shopcat_ route")
        if missing:
            ok = False
            print(f"STALE/INCOMPLETE  {path}  (missing: {', '.join(missing)})")
        else:
            print(f"OK   {path}  ({len(src.splitlines())} lines)")
    except FileNotFoundError:
        ok = False; print(f"MISSING  {path}")
    except SyntaxError as e:
        ok = False; print(f"TRUNCATED  {path}  (syntax error: {e}) -- re-upload fully")

js = "web_admin/static/js/app.js"
try:
    src = open(js, encoding="utf-8").read()
    missing = [n for n in ("manageCategories", "createCategory", "productCatFilter") if n not in src]
    if missing:
        ok = False; print(f"STALE  {js}  (missing: {', '.join(missing)})")
    else:
        print(f"OK   {js}")
except FileNotFoundError:
    ok = False; print(f"MISSING  {js}")

print()
print("ALL FILES OK -- safe to restart the bot" if ok else "Fix the files above, then restart")
sys.exit(0 if ok else 1)
