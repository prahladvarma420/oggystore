from database.db import cur, conn, get_setting, set_user_coupon
from utils.timezone import now_text
from config import ADMIN_ID


async def run_auto_marketing(context):
    try:
        coupon = get_setting("inactive_coupon", "")
        days = int(get_setting("inactive_days", "7") or 7)
        if not coupon:
            return

        cur.execute("""
            SELECT user_id
            FROM users
            WHERE COALESCE(blocked,0)=0
              AND (last_marketing_sent IS NULL OR DATE(last_marketing_sent) < DATE('now'))
              AND user_id NOT IN (
                  SELECT DISTINCT user_id FROM orders WHERE DATE(created) >= DATE('now', ?)
              )
            LIMIT 25
        """, (f"-{days} days",))
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
