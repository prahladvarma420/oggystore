import html
import logging
from pathlib import Path
from datetime import datetime

LOG_DIR = Path("logs")
ERROR_LOG_FILE = LOG_DIR / "error.log"

# Attach a handler to OUR logger only. logging.basicConfig() at import time
# hijacked the root logger for every third-party library, and LOG_DIR.mkdir()
# at import crashed outright on a read-only filesystem.
logger = logging.getLogger("oggystore_bot")
logger.setLevel(logging.ERROR)
logger.propagate = False

if not logger.handlers:
    _formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    try:
        LOG_DIR.mkdir(exist_ok=True)
        _handler = logging.FileHandler(ERROR_LOG_FILE, encoding="utf-8")
    except Exception:
        # Read-only or ephemeral FS: fall back to stderr instead of dying.
        _handler = logging.StreamHandler()
    _handler.setFormatter(_formatter)
    logger.addHandler(_handler)


def log_error(title: str, exc: Exception = None, extra: str = ""):
    from utils.email_privacy import active
    if active():
        logger.error("API email fulfillment: automation error")
        return
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

    if update.message is None or update.message.from_user is None:
        return
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

    # Transient network blips between the bot and Telegram's API (a connection
    # dropped mid-poll) are self-healing — log quietly and don't DM the admin.
    err_name = type(error).__name__ if error else ""
    transient = (
        "ReadError", "ConnectError", "ConnectTimeout", "ReadTimeout",
        "WriteError", "WriteTimeout", "PoolTimeout", "RemoteProtocolError",
        "NetworkError", "TimedOut", "ConnectionError",
    )
    if err_name in transient:
        log_error("Transient network error (ignored)", error)
        return

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
