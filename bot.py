import sys

from tasks.auto_marketing import run_auto_marketing
from tasks.scheduled_broadcasts import run_scheduled_broadcasts
from services.m365_family import run_m365_maintenance
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from config import BOT_TOKEN, PAYMENT_CHECK_INTERVAL_SECONDS, BACKUP_INTERVAL_SECONDS
from database.db import init_db
from handlers.user_handlers import start, admin, buttons, message_handler, photo_handler, document_handler
from payments.bep20_checker import check_payments
from payments.trc20_checker import check_trc20_payments
from payments.binance_checker import check_binance_payments
from tasks.invoice_reminders import run_invoice_reminders
from tasks.ticket_reminders import run_ticket_reminders
from admin.backup_handler import backup_command
from admin.admin_handlers import block_command, unblock_command, add_admin_command, remove_admin_command, admins_command, reply_ticket_command, close_ticket_command
from utils.backup import scheduled_backup
from utils.error_logger import error_logs_command, global_error_handler
from handlers.reseller_handlers import reseller_command


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    if app.job_queue:
        app.job_queue.run_repeating(run_auto_marketing, interval=3600, first=60)
    if app.job_queue:
        app.job_queue.run_repeating(run_scheduled_broadcasts, interval=60, first=10)
    if app.job_queue:
        # M365 keep-alive refresher + early-warning + queued-order auto-deliver.
        # Runs hourly; first pass 1 min after boot (frequent refresh keeps
        # Microsoft sessions alive longer and reduces idle expiries).
        app.job_queue.run_repeating(run_m365_maintenance, interval=3600, first=60)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reseller", reseller_command))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CommandHandler("block", block_command))
    app.add_handler(CommandHandler("unblock", unblock_command))
    app.add_handler(CommandHandler("addadmin", add_admin_command))
    app.add_handler(CommandHandler("removeadmin", remove_admin_command))
    app.add_handler(CommandHandler("admins", admins_command))
    app.add_handler(CommandHandler("replyticket", reply_ticket_command))
    app.add_handler(CommandHandler("closeticket", close_ticket_command))
    app.add_handler(CommandHandler("errorlogs", error_logs_command))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_error_handler(global_error_handler)

    if app.job_queue:
        app.job_queue.run_repeating(
            check_payments,
            interval=PAYMENT_CHECK_INTERVAL_SECONDS,
            first=10,
        )
        app.job_queue.run_repeating(
            check_trc20_payments,
            interval=PAYMENT_CHECK_INTERVAL_SECONDS,
            first=20,
        )
        app.job_queue.run_repeating(
            check_binance_payments,
            interval=PAYMENT_CHECK_INTERVAL_SECONDS,
            first=25,
        )
        # Abandoned-invoice reminder: nudge users ~1h before invoice expiry.
        app.job_queue.run_repeating(
            run_invoice_reminders,
            interval=600,
            first=120,
        )
        # Unresolved support-ticket reminders: nudge the admin every 24h while
        # a customer conversation still needs attention. Deduped persistently
        # via tickets.last_reminder_at, so restarts never resend.
        app.job_queue.run_repeating(
            run_ticket_reminders,
            interval=3600,
            first=180,
        )
        # Automatic database backup (was imported but never registered).
        app.job_queue.run_repeating(
            scheduled_backup,
            interval=BACKUP_INTERVAL_SECONDS,
            first=300,
        )

    print("[bot] Started — polling for updates. Payment auto-checker is running.", flush=True)
    try:
        app.run_polling(drop_pending_updates=True)
    except Exception as exc:
        print(f"[bot] FATAL: polling crashed: {exc!r}", flush=True)
    finally:
        print("[bot] Stopped.", flush=True)
    # run_polling never returns during normal operation, so reaching this
    # point means the bot died (e.g. "can't start new thread") or the host
    # asked it to stop. Exit non-zero so Railway's on-failure restart policy
    # brings the store back automatically instead of leaving it offline for
    # hours until someone notices and redeploys by hand. During a redeploy
    # or manual stop the container is being replaced anyway, so the exit
    # code is irrelevant there.
    sys.exit(1)


if __name__ == "__main__":
    main()
