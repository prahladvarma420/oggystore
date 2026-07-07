from telegram.ext import ContextTypes
from config import ADMIN_ID
from utils.backup import create_backup
from database.db import log_admin_action


async def backup_command(update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_id != ADMIN_ID:
        await update.message.reply_text(
            "❌ <b>Admin only.</b>",
            parse_mode="HTML"
        )
        return

    try:
        backup_path = create_backup()
        log_admin_action(user_id, "CREATE_BACKUP", backup_path)

        await update.message.reply_text(
            f"""✅ <b>Backup Created Successfully</b>

━━━━━━━━━━━━━━━

📦 <b>File:</b>
<code>{backup_path}</code>

━━━━━━━━━━━━━━━

<i>Your database backup is saved safely inside the backups folder.</i>""",
            parse_mode="HTML"
        )

    except Exception as exc:
        await update.message.reply_text(
            f"""❌ <b>Backup Failed</b>

<code>{exc}</code>""",
            parse_mode="HTML"
        )
