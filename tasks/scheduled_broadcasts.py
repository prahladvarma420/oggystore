import asyncio

from telegram.error import RetryAfter

from database.db import cur, get_pending_scheduled_broadcasts, mark_scheduled_broadcast_done
from utils.timezone import now_text
from config import ADMIN_ID


async def run_scheduled_broadcasts(context):
    try:
        rows = get_pending_scheduled_broadcasts(now_text())
        for bid, message in rows:
            cur.execute("SELECT user_id FROM users WHERE COALESCE(blocked,0)=0 AND COALESCE(notifications,1)=1")
            users = cur.fetchall()
            sent = 0
            failed = 0
            # Materialise before awaiting: the shared cursor is reused by the
            # next loop iteration's query.
            targets = [row[0] for row in users]
            for target_id in targets:
                try:
                    await context.bot.send_message(chat_id=target_id, text=message, parse_mode="HTML")
                    sent += 1
                except RetryAfter as exc:
                    # Telegram flood control: wait, then retry once.
                    try:
                        await asyncio.sleep(float(getattr(exc, "retry_after", 5)) + 1)
                        await context.bot.send_message(chat_id=target_id, text=message, parse_mode="HTML")
                        sent += 1
                    except Exception:
                        failed += 1
                except Exception:
                    failed += 1
                # Stay under Telegram's ~30 msg/sec limit.
                await asyncio.sleep(0.05)
            mark_scheduled_broadcast_done(bid, sent, failed)
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"✅ Scheduled broadcast #{bid} sent.\nSent: {sent}\nFailed: {failed}",
                    parse_mode="HTML"
                )
            except Exception:
                pass
    except Exception:
        pass
