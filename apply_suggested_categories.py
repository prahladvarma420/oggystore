#!/usr/bin/env python3
"""One-time helper: create the recommended categories and auto-assign your
existing products by name keywords. Safe to re-run any number of times --
existing categories are reused and already-categorized products are skipped.

Usage (from the project root, next to bot.py):
    python3 apply_suggested_categories.py            # apply (skip categorized)
    python3 apply_suggested_categories.py --force    # also re-assign categorized products
    python3 apply_suggested_categories.py --dry-run  # preview only, no writes

You can fine-tune everything afterwards in Web Admin -> Products -> Categories.
"""
import sys

from database.db import init_db, cur, get_categories, create_category, assign_product_category

# (icon, category name, keywords matched against lowercased product names).
# Order matters: the first matching category wins.
SUGGESTED = [
    ("📺", "Streaming", ["amazon prime", "prime video", "netflix", "youtube"]),
    ("🎵", "Music", ["spotify"]),
    ("🤖", "AI Tools", ["claude", "chatgpt", "openai", "gpt", "lovable"]),
    ("💻", "Software & Productivity", ["microsoft 365", "office 365", "ms office", "jetbrains", "ilovepdf", "notion", "replit", "windows"]),
    ("🎨", "Design & Creative", ["canva", "adobe", "capcut", "photoshop"]),
    ("🎓", "Education", ["edx", "headspace", "udemy", "coursera"]),
    ("💼", "Business", ["linkedin"]),
    ("🔐", "VPN & Security", ["nordvpn", "nord vpn", "expressvpn", "vpn"]),
]


def main():
    dry = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    init_db()

    existing = {name.strip().lower(): cid for cid, name, *_ in get_categories()}
    cat_ids = {}
    for icon, name, _kw in SUGGESTED:
        cid = existing.get(name.lower())
        if cid is None:
            if dry:
                print(f"[dry] would create category {icon} {name}")
                cid = -len(cat_ids) - 1  # placeholder for the report
            else:
                cid = create_category(name, icon)
                print(f"created category {icon} {name} (#{cid})")
        else:
            print(f"reusing existing category {icon} {name} (#{cid})")
        cat_ids[name] = cid

    cur.execute("SELECT id, name, category_id FROM products")
    products = cur.fetchall()
    assigned = skipped = unmatched = 0
    for pid, pname, cat_id in products:
        low = (pname or "").lower()
        target = None
        for _icon, name, keywords in SUGGESTED:
            if any(k in low for k in keywords):
                target = cat_ids[name]
                break
        if target is None:
            unmatched += 1
            if dry:
                print(f"[dry] no match -> stays in Other: {pname}")
            continue
        if cat_id and not force:
            skipped += 1
            continue
        if dry:
            print(f"[dry] would assign #{pid} {pname} -> {name}")
        else:
            assign_product_category(pid, target)
        assigned += 1

    print()
    print(f"done: {assigned} {'would be ' if dry else ''}assigned, "
          f"{skipped} already categorized (skipped), {unmatched} unmatched -> stay in Other")
    if dry:
        print("dry run only -- re-run without --dry-run to apply")


if __name__ == "__main__":
    main()
