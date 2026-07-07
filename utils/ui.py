from utils.error_logger import log_error
from telegram.error import BadRequest

# Telegram messages that are NOT real problems - safe to ignore silently.
_BENIGN = ("not modified",)
# Cases where the message simply can't be edited (e.g. a photo/media message
# with no text) - we quietly resend instead of logging an error.
_NO_EDIT = (
    "no text in the message to edit",
    "there is no text in the message",
    "message can't be edited",
    "message to edit not found",
)


async def edit_or_send(q, text, markup=None, parse_mode="HTML"):
    """Edit the message behind a callback query, or send a fresh one.

    Robust against the common Telegram quirks:
    - "message is not modified"  -> ignored (identical content).
    - "no text in the message to edit" / media messages -> resend instead.
    - missing/None message (e.g. very old or inline messages) -> resend/skip.
    Only genuinely unexpected failures are logged.
    """
    try:
        await q.edit_message_text(
            text,
            reply_markup=markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
        return
    except BadRequest as exc:
        msg = str(exc).lower()
        if any(b in msg for b in _BENIGN):
            # Content is identical - nothing to do, and not an error.
            return
        if not any(n in msg for n in _NO_EDIT):
            # Unexpected edit failure - log it, then fall through to resend.
            log_error("edit_or_send edit failed", exc)
    except Exception as exc:
        log_error("edit_or_send edit failed", exc)

    # Fallback: send a brand new message (covers media messages, deleted
    # originals, etc.).
    target = getattr(q, "message", None)
    if target is None:
        return
    try:
        await target.reply_text(
            text,
            reply_markup=markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
    except Exception as reply_exc:
        log_error("edit_or_send reply failed", reply_exc)
