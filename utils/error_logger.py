import html
import logging
from pathlib import Path
from datetime import datetime

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

ERROR_LOG_FILE = LOG_DIR / "error.log"

logging.basicConfig(
    filename=ERROR_LOG_FILE,
    level=logging.ERROR,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    encoding="utf-8"
)

logger = logging.getLogger("oggystore_bot")


def log_error(title: str, exc: Exception = None, extra: str = ""):
    message = f"{title}"

    if extra:
        message += f" | {extra}"

    if exc:
        logger.exception(message)
    else:
        logger.error(message)


def get_recent_errors(limit: int = 40) -> str:
    if not ERROR_LOG_FILE.exists():
        return "No error logs found."

    try:
        lines = ERROR_LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
        recent = lines[-limit:]

        if not recent:
            return "No error logs found."

        return "\n".join(recent)
    except Exception as exc:
        return f"Could not read error log: {exc}"


async def error_logs_command(update, context):
    from config import ADMIN_ID

    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("❌ <b>Admin only.</b>", parse_mode="HTML")
        return

    logs = get_recent_errors(30)

    if len(logs) > 3800:
        logs = logs[-3800:]

    await update.message.reply_text(
        f"🧾 <b>Recent Error Logs</b>\n\n<code>{html.escape(logs, quote=False)}</code>",
        parse_mode="HTML"
    )


async def global_error_handler(update, context):
    error = context.error
    log_error("Unhandled bot error", error, extra=f"Update: {update}")

    try:
        from config import ADMIN_ID
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"⚠️ <b>Bot Error Logged</b>\n\n<code>{html.escape(str(error), quote=False)}</code>",
            parse_mode="HTML"
        )
    except Exception:
        pass
