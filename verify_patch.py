"""Run from the project root (where bot.py is):  python3 verify_patch.py
Verifies the user-search crash fix + users pagination patch is fully uploaded."""
import ast, sys

CHECKS = {
    "database/db.py": ["get_user_overview", "get_category_by_name", "find_user_by_id"],
    "admin/admin_handlers.py": ["_admin_user_card_text", "get_order_detail_text",
                                "handle_admin_message", "handle_admin_callback"],
    "web_admin/app.py": ["api_users", "api_user_detail"],
}
TEXT_CHECKS = {
    "database/db.py": ["user = tuple(user)"],
    "admin/admin_handlers.py": ["list(overview[\"user\"])[:6]",
                                "html.escape(str(first_name or 'User')",
                                "html.escape(str(delivery or \"\")"],
    "web_admin/app.py": ["per_page", '"pages": pages'],
    "web_admin/static/js/app.js": ["usersGo", "usersLastSearch"],
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
