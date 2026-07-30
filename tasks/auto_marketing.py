from database.db import cur, conn, get_setting, set_user_coupon
from utils.timezone import now_text, today_prefix
from config import ADMIN_ID


def _cutoff_text(days):
    """UTC-naive cutoff string. DATE('now', '-N days') is SQLite-only and
    silently failed on Postgres, so inactive-user marketing never ran."""
    from datetime import timedelta
    try:
        from utils.timezone import now as _now
        base = _now()
    except Exception:
        from datetime import datetime
        base = datetime.now()
    return (base - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")



async def run_auto_marketing(context):
    # Auto-expire flash sales every hour so product prices return to normal
    # on time, even if the admin never presses the manual Cleanup button.
    try:
        cur.execute(
            "UPDATE products SET flash_price=NULL, flash_ends_at=NULL "
            "WHERE flash_ends_at IS NOT NULL AND flash_ends_at < ?",
            (now_text(),),
        )
        if cur.rowcount:
            conn.commit()
    except Exception:
        pass

    try:
        coupon = get_setting("inactive_coupon", "")
        days = int(get_setting("inactive_days", "7") or 7)
        if not coupon:
            return

        cur.execute("""
            SELECT user_id
            FROM users
            WHERE COALESCE(blocked,0)=0
              AND (last_marketing_sent IS NULL OR last_marketing_sent < ?)
              AND user_id NOT IN (
                  SELECT DISTINCT user_id FROM orders WHERE created >= ?
              )
            LIMIT 25
        """, (today_prefix(), _cutoff_text(days)))
        users = cur.fetchall()

        sent = 0
        for row in users:
            user_id = row[0]
            try:
                set_user_coupon(user_id, coupon)
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🎁 We miss you!\n\nUse coupon <code>{coupon}</code> on your next checkout.",
                    parse_mode="HTML"
                )
                cur.execute("UPDATE users SET last_marketing_sent=? WHERE user_id=?", (now_text(), user_id))
                conn.commit()
                sent += 1
            except Exception:
                pass

        if sent:
            try:
                await context.bot.send_message(chat_id=ADMIN_ID, text=f"🤖 Auto marketing sent to {sent} inactive users.", parse_mode="HTML")
            except Exception:
                pass
    except Exception:
        pass
