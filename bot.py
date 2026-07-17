from tasks.auto_marketing import run_auto_marketing
from tasks.scheduled_broadcasts import run_scheduled_broadcasts
from services.m365_family import run_m365_maintenance
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from config import BOT_TOKEN, PAYMENT_CHECK_INTERVAL_SECONDS, BACKUP_INTERVAL_SECONDS
from database.db import init_db
from handlers.user_handlers import start, admin, buttons, message_handler, photo_handler, document_handler
from payments.bep20_checker import check_payments
from admin.backup_handler import backup_command
from admin.admin_handlers import block_command, unblock_command, add_admin_command, remove_admin_command, admins_command, reply_ticket_command, close_ticket_command
from utils.backup import scheduled_backup
from utils.error_logger import error_logs_command, global_error_handler


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

    print("Bot Running...")
    print("Auto-payment checker enabled using RPC/Web3. No BscScan API key needed.")
    app.run_polling()


if __name__ == "__main__":
    main()
